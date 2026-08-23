const http = require('http');
const fs = require('fs');
const path = require('path');

const root = __dirname;
const port = Number(process.env.BAGS_UI_PORT || 4173);
const apiTarget = process.env.BAGS_API_TARGET ? new URL(process.env.BAGS_API_TARGET) : null;

http.createServer((request, response) => {
  const requestUrl = new URL(request.url, 'http://127.0.0.1');
  const pathname = requestUrl.pathname === '/' ? '/index.html' : requestUrl.pathname;

  if (apiTarget && pathname.startsWith('/api/v1/')) {
    const upstream = http.request(new URL(`${pathname}${requestUrl.search}`, apiTarget), {
      method: request.method,
      headers: { ...request.headers, host: apiTarget.host },
    }, (upstreamResponse) => {
      response.writeHead(upstreamResponse.statusCode || 502, upstreamResponse.headers);
      upstreamResponse.pipe(response);
    });
    upstream.on('error', () => {
      if (!response.headersSent) response.writeHead(502, { 'Content-Type': 'application/json; charset=utf-8' });
      response.end(JSON.stringify({ detail: '本地 API 代理暂时不可用' }));
    });
    request.pipe(upstream);
    return;
  }

  const filePath = path.resolve(root, `.${pathname}`);

  if (!filePath.startsWith(root)) {
    response.writeHead(403);
    response.end('Forbidden');
    return;
  }

  fs.readFile(filePath, (error, content) => {
    if (error) {
      response.writeHead(404);
      response.end('Not found');
      return;
    }

    const types = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml' };
    response.writeHead(200, {
      'Content-Type': types[path.extname(filePath)] || 'application/octet-stream',
      'X-Content-Type-Options': 'nosniff',
      'Referrer-Policy': 'same-origin',
      'Content-Security-Policy': "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; connect-src 'self' http://127.0.0.1:8000 http://localhost:8000; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
    });
    response.end(content);
  });
}).listen(port, '127.0.0.1', () => {
  console.log(`Bags UI preview: http://127.0.0.1:${port}`);
});
