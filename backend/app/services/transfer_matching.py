import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import (
    Account,
    Asset,
    EntryDirection,
    EventStatus,
    LedgerEntry,
    LedgerEvent,
    LedgerEventType,
    Portfolio,
    RawEvent,
    SyncRunStatus,
    TransferCandidate,
    TransferCandidateStatus,
    TransferGroup,
    TransferGroupStatus,
    TransferMatchRun,
    utc_now,
)
from app.schemas import TransferMatchRequest
from app.services.security import as_utc

SOURCE_TYPES = {LedgerEventType.WITHDRAW, LedgerEventType.TRANSFER_OUT}
DESTINATION_TYPES = {LedgerEventType.DEPOSIT, LedgerEventType.TRANSFER_IN}
MUTABLE_CANDIDATE_STATUSES = {TransferCandidateStatus.UNMATCHED, TransferCandidateStatus.NEEDS_REVIEW}


@dataclass
class TransferLeg:
    event: LedgerEvent
    account: Account
    asset: Asset
    amount: Decimal
    explicit_fee: Decimal
    fee_amount: Decimal
    fee_asset_id: UUID | None
    tx_hash: str | None
    source_identifier: str | None


class TransferMatchStats:
    def __init__(self) -> None:
        self.sources_scanned = 0
        self.destinations_scanned = 0
        self.candidates_created = 0
        self.candidates_updated = 0
        self.automatically_matched = 0
        self.needs_review = 0
        self.unmatched_candidates = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "sources_scanned": self.sources_scanned,
            "destinations_scanned": self.destinations_scanned,
            "candidates_created": self.candidates_created,
            "candidates_updated": self.candidates_updated,
            "automatically_matched": self.automatically_matched,
            "needs_review": self.needs_review,
            "unmatched_candidates": self.unmatched_candidates,
        }


class TransferMatchingService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.stats = TransferMatchStats()
        self.warnings: list[str] = []

    def run(self, portfolio_id: UUID, request: TransferMatchRequest) -> TransferMatchRun:
        if not self.session.get(Portfolio, portfolio_id):
            raise ValueError("portfolio not found")
        run = TransferMatchRun(portfolio_id=portfolio_id)
        self.session.add(run)
        self.session.commit()
        self.session.refresh(run)
        try:
            sources, destinations = self._load_legs(portfolio_id, request)
            self.stats.sources_scanned = len(sources)
            self.stats.destinations_scanned = len(destinations)
            candidates = self._score_and_persist(portfolio_id, sources, destinations)
            self._auto_match(candidates)
            self.session.commit()
            status = SyncRunStatus.PARTIAL if self.warnings else SyncRunStatus.SUCCEEDED
            self._finish(run.id, status)
        except Exception as error:
            self.session.rollback()
            self._finish(run.id, SyncRunStatus.FAILED, "TRANSFER_MATCH_ERROR", str(error)[:300])
        return self.session.get(TransferMatchRun, run.id)  # type: ignore[return-value]

    def confirm_candidate(self, candidate_id: UUID, note: str | None = None) -> TransferGroup:
        candidate = self.session.get(TransferCandidate, candidate_id)
        if not candidate:
            raise ValueError("transfer candidate not found")
        if candidate.status == TransferCandidateStatus.IGNORED:
            raise ValueError("ignored candidate must be restored before matching")
        source = self._leg_for_event(candidate.source_event_id, source=True)
        destination = self._leg_for_event(candidate.destination_event_id, source=False)
        group = self._create_group(
            candidate,
            source,
            destination,
            status=TransferGroupStatus.CONFIRMED,
            method="manual_confirmation",
            note=note,
        )
        candidate.status = TransferCandidateStatus.CONFIRMED
        self.session.commit()
        self.session.refresh(group)
        return group

    def manual_match(self, source_event_id: UUID, destination_event_id: UUID, note: str | None = None) -> TransferGroup:
        source = self._leg_for_event(source_event_id, source=True)
        destination = self._leg_for_event(destination_event_id, source=False)
        if source.event.portfolio_id != destination.event.portfolio_id:
            raise ValueError("transfer events must belong to the same portfolio")
        if source.account.id == destination.account.id:
            raise ValueError("transfer events must belong to different accounts")
        score, breakdown, fee = self._score(source, destination)
        candidate = self.session.scalar(
            select(TransferCandidate).where(
                TransferCandidate.source_event_id == source_event_id,
                TransferCandidate.destination_event_id == destination_event_id,
            )
        )
        if not candidate:
            candidate = TransferCandidate(
                portfolio_id=source.event.portfolio_id,
                source_event_id=source.event.id,
                destination_event_id=destination.event.id,
                source_account_id=source.account.id,
                destination_account_id=destination.account.id,
                source_asset_id=source.asset.id,
                destination_asset_id=destination.asset.id,
                source_amount=source.amount,
                destination_amount=destination.amount,
                estimated_fee_amount=fee,
                score=score,
                score_breakdown_json=breakdown,
                status=TransferCandidateStatus.CONFIRMED,
            )
            self.session.add(candidate)
            self.session.flush()
        else:
            candidate.status = TransferCandidateStatus.CONFIRMED
        group = self._create_group(
            candidate,
            source,
            destination,
            status=TransferGroupStatus.CONFIRMED,
            method="manual",
            note=note,
        )
        self.session.commit()
        self.session.refresh(group)
        return group

    def unmatch(self, group_id: UUID, note: str | None = None) -> TransferGroup:
        group = self.session.get(TransferGroup, group_id)
        if not group or group.status == TransferGroupStatus.UNMATCHED:
            raise ValueError("active transfer group not found")
        group.status = TransferGroupStatus.UNMATCHED
        group.note = note or group.note
        group.metadata_json = {**(group.metadata_json or {}), "unmatched_at": utc_now().isoformat()}
        if group.candidate_id:
            candidate = self.session.get(TransferCandidate, group.candidate_id)
            if candidate:
                candidate.status = TransferCandidateStatus.REJECTED
        self.session.commit()
        self.session.refresh(group)
        return group

    def ignore_candidate(self, candidate_id: UUID) -> TransferCandidate:
        candidate = self.session.get(TransferCandidate, candidate_id)
        if not candidate:
            raise ValueError("transfer candidate not found")
        if self._active_group_for_event(candidate.source_event_id) or self._active_group_for_event(candidate.destination_event_id):
            raise ValueError("matched candidate must be unmatched before it can be ignored")
        candidate.status = TransferCandidateStatus.IGNORED
        self.session.commit()
        self.session.refresh(candidate)
        return candidate

    def _load_legs(self, portfolio_id: UUID, request: TransferMatchRequest) -> tuple[list[TransferLeg], list[TransferLeg]]:
        statement = (
            select(LedgerEvent)
            .where(
                LedgerEvent.portfolio_id == portfolio_id,
                LedgerEvent.status == EventStatus.POSTED,
                LedgerEvent.event_type.in_([*SOURCE_TYPES, *DESTINATION_TYPES]),
            )
            .order_by(LedgerEvent.occurred_at.desc(), LedgerEvent.id.desc())
            .limit(self.settings.transfer_match_max_events)
        )
        if request.history_start:
            statement = statement.where(LedgerEvent.occurred_at >= request.history_start)
        if request.history_end:
            statement = statement.where(LedgerEvent.occurred_at <= request.history_end)
        events = list(self.session.scalars(statement))
        if len(events) >= self.settings.transfer_match_max_events:
            self.warnings.append(f"Transfer matching was limited to {self.settings.transfer_match_max_events} ledger events.")
        event_ids = [event.id for event in events]
        entries_by_event: dict[UUID, list[LedgerEntry]] = defaultdict(list)
        raw_ids = [event.raw_event_id for event in events if event.raw_event_id]
        if event_ids:
            for entry in self.session.scalars(select(LedgerEntry).where(LedgerEntry.ledger_event_id.in_(event_ids))):
                entries_by_event[entry.ledger_event_id].append(entry)
        raw_map = {
            item.id: item for item in self.session.scalars(select(RawEvent).where(RawEvent.id.in_(raw_ids)))
        } if raw_ids else {}
        account_ids = {entry.account_id for values in entries_by_event.values() for entry in values}
        asset_ids = {entry.asset_id for values in entries_by_event.values() for entry in values}
        accounts = {item.id: item for item in self.session.scalars(select(Account).where(Account.id.in_(account_ids)))} if account_ids else {}
        assets = {item.id: item for item in self.session.scalars(select(Asset).where(Asset.id.in_(asset_ids)))} if asset_ids else {}

        sources: list[TransferLeg] = []
        destinations: list[TransferLeg] = []
        for event in events:
            is_source = event.event_type in SOURCE_TYPES
            direction = EntryDirection.DEBIT if is_source else EntryDirection.CREDIT
            non_fee = [entry for entry in entries_by_event[event.id] if not entry.fee_flag and entry.direction == direction]
            grouped: dict[tuple[UUID, UUID], Decimal] = defaultdict(lambda: Decimal("0"))
            for entry in non_fee:
                grouped[(entry.account_id, entry.asset_id)] += entry.quantity
            if len(grouped) != 1:
                self.warnings.append(f"Ledger event {event.id} was skipped because its transfer asset/account is ambiguous.")
                continue
            (account_id, asset_id), amount = next(iter(grouped.items()))
            account = accounts.get(account_id)
            asset = assets.get(asset_id)
            if not account or not asset or amount <= 0:
                continue
            fee_entries = [entry for entry in entries_by_event[event.id] if entry.fee_flag and entry.direction == EntryDirection.DEBIT]
            same_asset_fee = sum((entry.quantity for entry in fee_entries if entry.asset_id == asset_id), Decimal("0"))
            fee_asset_id = fee_entries[0].asset_id if len({entry.asset_id for entry in fee_entries}) == 1 and fee_entries else None
            fee_amount = sum((entry.quantity for entry in fee_entries), Decimal("0")) if fee_asset_id else Decimal("0")
            if fee_entries and fee_asset_id is None:
                self.warnings.append(f"Ledger event {event.id} has fees in multiple assets; Transfer Group fee remains unassigned.")
            raw = raw_map.get(event.raw_event_id) if event.raw_event_id else None
            tx_hash = self._tx_hash(event, raw)
            identifier = self._source_identifier(raw)
            leg = TransferLeg(event, account, asset, amount, same_asset_fee, fee_amount, fee_asset_id, tx_hash, identifier)
            (sources if is_source else destinations).append(leg)
        return sources, destinations

    def _score_and_persist(
        self,
        portfolio_id: UUID,
        sources: list[TransferLeg],
        destinations: list[TransferLeg],
    ) -> list[TransferCandidate]:
        by_asset: dict[str, list[TransferLeg]] = defaultdict(list)
        by_hash: dict[str, list[TransferLeg]] = defaultdict(list)
        for destination in destinations:
            by_asset[self._asset_key(destination.asset)].append(destination)
            if destination.tx_hash:
                by_hash[destination.tx_hash].append(destination)
        candidates: list[TransferCandidate] = []
        seen_pairs: set[tuple[UUID, UUID]] = set()
        normal_window = timedelta(hours=self.settings.transfer_match_window_hours)
        hash_window = timedelta(days=self.settings.transfer_exact_hash_window_days)
        candidate_tolerance = Decimal(str(self.settings.transfer_amount_candidate_tolerance))
        for source in sources:
            potential = list(by_asset.get(self._asset_key(source.asset), []))
            if source.tx_hash:
                potential.extend(by_hash.get(source.tx_hash, []))
            for destination in potential:
                pair = (source.event.id, destination.event.id)
                if pair in seen_pairs or source.account.id == destination.account.id:
                    continue
                seen_pairs.add(pair)
                hash_match = bool(source.tx_hash and destination.tx_hash and source.tx_hash == destination.tx_hash)
                time_delta = abs(as_utc(destination.event.occurred_at) - as_utc(source.event.occurred_at))
                if time_delta > (hash_window if hash_match else normal_window):
                    continue
                asset_match = self._asset_key(source.asset) == self._asset_key(destination.asset)
                amount_error = self._amount_error(source, destination)
                if not hash_match and (not asset_match or amount_error > candidate_tolerance):
                    continue
                score, breakdown, estimated_fee = self._score(source, destination)
                status = self._status_for_score(score)
                candidate = self.session.scalar(
                    select(TransferCandidate).where(
                        TransferCandidate.source_event_id == source.event.id,
                        TransferCandidate.destination_event_id == destination.event.id,
                    )
                )
                if not candidate:
                    candidate = TransferCandidate(
                        portfolio_id=portfolio_id,
                        source_event_id=source.event.id,
                        destination_event_id=destination.event.id,
                        source_account_id=source.account.id,
                        destination_account_id=destination.account.id,
                        source_asset_id=source.asset.id,
                        destination_asset_id=destination.asset.id,
                        source_amount=source.amount,
                        destination_amount=destination.amount,
                        estimated_fee_amount=estimated_fee,
                        score=score,
                        score_breakdown_json=breakdown,
                        status=status,
                    )
                    self.session.add(candidate)
                    self.session.flush()
                    self.stats.candidates_created += 1
                elif candidate.status in MUTABLE_CANDIDATE_STATUSES:
                    candidate.source_amount = source.amount
                    candidate.destination_amount = destination.amount
                    candidate.estimated_fee_amount = estimated_fee
                    candidate.score = score
                    candidate.score_breakdown_json = breakdown
                    candidate.status = status
                    self.stats.candidates_updated += 1
                candidates.append(candidate)
                if status == TransferCandidateStatus.NEEDS_REVIEW:
                    self.stats.needs_review += 1
                elif status == TransferCandidateStatus.UNMATCHED:
                    self.stats.unmatched_candidates += 1
        return candidates

    def _auto_match(self, candidates: list[TransferCandidate]) -> None:
        active_groups = list(
            self.session.scalars(select(TransferGroup).where(TransferGroup.status != TransferGroupStatus.UNMATCHED))
        )
        used_events = {event_id for group in active_groups for event_id in (group.source_event_id, group.destination_event_id)}
        active_pairs = {(group.source_event_id, group.destination_event_id) for group in active_groups}
        eligible = [
            candidate
            for candidate in candidates
            if candidate.score >= self.settings.transfer_auto_score
            and candidate.status not in {
                TransferCandidateStatus.CONFIRMED,
                TransferCandidateStatus.REJECTED,
                TransferCandidateStatus.IGNORED,
            }
        ]
        eligible.sort(
            key=lambda item: (
                -item.score,
                item.score_breakdown_json.get("time_difference_seconds", 10**18),
                item.score_breakdown_json.get("amount_error_ratio", "1"),
                str(item.id),
            )
        )
        for candidate in eligible:
            if candidate.source_event_id in used_events or candidate.destination_event_id in used_events:
                if (candidate.source_event_id, candidate.destination_event_id) not in active_pairs:
                    candidate.status = TransferCandidateStatus.NEEDS_REVIEW
                    candidate.score_breakdown_json = {**candidate.score_breakdown_json, "automatic_match_blocked": "competing active match"}
                continue
            source = self._leg_for_event(candidate.source_event_id, source=True)
            destination = self._leg_for_event(candidate.destination_event_id, source=False)
            self._create_group(
                candidate,
                source,
                destination,
                status=TransferGroupStatus.AUTO_MATCHED,
                method="automatic",
            )
            candidate.status = TransferCandidateStatus.AUTO_MATCHED
            used_events.update({candidate.source_event_id, candidate.destination_event_id})
            self.stats.automatically_matched += 1

    def _create_group(
        self,
        candidate: TransferCandidate,
        source: TransferLeg,
        destination: TransferLeg,
        *,
        status: TransferGroupStatus,
        method: str,
        note: str | None = None,
    ) -> TransferGroup:
        # Lock both events before checking active membership. PostgreSQL then
        # serializes competing confirmation/matching requests sharing either leg.
        locked = list(
            self.session.scalars(
                select(LedgerEvent)
                .where(LedgerEvent.id.in_([source.event.id, destination.event.id]))
                .with_for_update()
            )
        )
        if len(locked) != 2:
            raise ValueError("transfer events no longer exist")
        conflict = self._active_group_for_event(source.event.id) or self._active_group_for_event(destination.event.id)
        if conflict:
            if conflict.source_event_id == source.event.id and conflict.destination_event_id == destination.event.id:
                if status == TransferGroupStatus.CONFIRMED:
                    conflict.status = status
                    conflict.match_method = method
                    conflict.note = note or conflict.note
                    conflict.candidate_id = candidate.id
                    conflict.confidence_score = candidate.score
                return conflict
            raise ValueError("one of the transfer events already belongs to an active transfer group")
        tx_hash = source.tx_hash if source.tx_hash and source.tx_hash == destination.tx_hash else source.tx_hash or destination.tx_hash
        group = TransferGroup(
            reference=f"TRF_{uuid.uuid4().hex[:12].upper()}",
            portfolio_id=source.event.portfolio_id,
            candidate_id=candidate.id,
            source_event_id=source.event.id,
            destination_event_id=destination.event.id,
            source_account_id=source.account.id,
            destination_account_id=destination.account.id,
            source_asset_id=source.asset.id,
            destination_asset_id=destination.asset.id,
            source_amount=source.amount,
            destination_amount=destination.amount,
            fee_amount=source.fee_amount if source.fee_amount > 0 else candidate.estimated_fee_amount,
            fee_asset_id=source.fee_asset_id if source.fee_amount > 0 else (source.asset.id if candidate.estimated_fee_amount > 0 else None),
            tx_hash=tx_hash,
            withdrawal_id=source.source_identifier,
            deposit_id=destination.source_identifier,
            source_occurred_at=source.event.occurred_at,
            destination_occurred_at=destination.event.occurred_at,
            status=status,
            confidence_score=candidate.score,
            match_method=method,
            note=note,
            metadata_json={
                "score_breakdown": candidate.score_breakdown_json,
                "source_asset": source.asset.canonical_symbol,
                "destination_asset": destination.asset.canonical_symbol,
                "internal_portfolio_transfer": True,
            },
        )
        self.session.add(group)
        self.session.flush()
        return group

    def _leg_for_event(self, event_id: UUID, *, source: bool) -> TransferLeg:
        event = self.session.get(LedgerEvent, event_id)
        expected = SOURCE_TYPES if source else DESTINATION_TYPES
        if not event or event.status != EventStatus.POSTED or event.event_type not in expected:
            raise ValueError("ledger event is not an eligible transfer leg")
        request = TransferMatchRequest(history_start=event.occurred_at - timedelta(seconds=1), history_end=event.occurred_at + timedelta(seconds=1))
        sources, destinations = self._load_legs(event.portfolio_id, request)
        values = sources if source else destinations
        leg = next((item for item in values if item.event.id == event_id), None)
        if not leg:
            raise ValueError("ledger event has ambiguous or missing transfer entries")
        return leg

    def _score(self, source: TransferLeg, destination: TransferLeg) -> tuple[int, dict[str, Any], Decimal]:
        score = 0
        hash_match = bool(source.tx_hash and destination.tx_hash and source.tx_hash == destination.tx_hash)
        asset_match = self._asset_key(source.asset) == self._asset_key(destination.asset)
        amount_error = self._amount_error(source, destination)
        time_seconds = int(abs((as_utc(destination.event.occurred_at) - as_utc(source.event.occurred_at)).total_seconds()))
        if hash_match:
            score += 60
        if asset_match:
            score += 20
        if amount_error < Decimal(str(self.settings.transfer_amount_close_tolerance)):
            score += 10
        if time_seconds < self.settings.transfer_time_close_minutes * 60:
            score += 10
        estimated_fee = source.explicit_fee if source.explicit_fee > 0 else max(source.amount - destination.amount, Decimal("0"))
        breakdown = {
            "tx_hash_match": hash_match,
            "tx_hash_points": 60 if hash_match else 0,
            "asset_match": asset_match,
            "asset_points": 20 if asset_match else 0,
            "amount_error_ratio": str(amount_error),
            "amount_points": 10 if amount_error < Decimal(str(self.settings.transfer_amount_close_tolerance)) else 0,
            "time_difference_seconds": time_seconds,
            "time_points": 10 if time_seconds < self.settings.transfer_time_close_minutes * 60 else 0,
            "explicit_fee": str(source.explicit_fee),
            "reported_fee_amount": str(source.fee_amount),
            "reported_fee_asset_id": str(source.fee_asset_id) if source.fee_asset_id else None,
        }
        return score, breakdown, estimated_fee

    @staticmethod
    def _asset_key(asset: Asset) -> str:
        return str(asset.underlying_asset_id or asset.canonical_symbol.strip().upper())

    @staticmethod
    def _amount_error(source: TransferLeg, destination: TransferLeg) -> Decimal:
        if source.amount <= 0:
            return Decimal("1")
        candidates = [abs(source.amount - destination.amount)]
        if source.explicit_fee > 0 and source.amount > source.explicit_fee:
            candidates.append(abs((source.amount - source.explicit_fee) - destination.amount))
        return min(candidates) / source.amount

    def _status_for_score(self, score: int) -> TransferCandidateStatus:
        if score >= self.settings.transfer_auto_score:
            return TransferCandidateStatus.AUTO_MATCHED
        if score >= self.settings.transfer_review_score:
            return TransferCandidateStatus.NEEDS_REVIEW
        return TransferCandidateStatus.UNMATCHED

    def _active_group_for_event(self, event_id: UUID) -> TransferGroup | None:
        return self.session.scalar(
            select(TransferGroup).where(
                TransferGroup.status != TransferGroupStatus.UNMATCHED,
                (TransferGroup.source_event_id == event_id) | (TransferGroup.destination_event_id == event_id),
            )
        )

    @staticmethod
    def _tx_hash(event: LedgerEvent, raw: RawEvent | None) -> str | None:
        values: list[Any] = [event.tx_hash]
        payload = raw.payload_json if raw and isinstance(raw.payload_json, dict) else {}
        transaction = payload.get("transaction") if isinstance(payload.get("transaction"), dict) else {}
        values.extend([payload.get("txId"), payload.get("txHash"), payload.get("transactionHash"), transaction.get("hash")])
        for value in values:
            normalized = str(value or "").strip().lower()
            if normalized:
                return normalized
        return None

    @staticmethod
    def _source_identifier(raw: RawEvent | None) -> str | None:
        payload = raw.payload_json if raw and isinstance(raw.payload_json, dict) else {}
        for key in ("id", "withdrawOrderId", "withdrawalId", "depositId"):
            value = payload.get(key)
            if value is not None and str(value).strip():
                return str(value)[:256]
        return raw.external_event_id[:256] if raw else None

    def _finish(
        self,
        run_id: UUID,
        status: SyncRunStatus,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        run = self.session.get(TransferMatchRun, run_id)
        if not run:
            return
        run.status = status
        run.stats_json = self.stats.as_dict()
        run.warnings_json = self.warnings
        run.error_code = error_code
        run.error_message = error_message
        run.finished_at = utc_now()
        self.session.commit()
