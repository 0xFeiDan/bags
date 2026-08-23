FROM node:22-alpine

ENV NODE_ENV=production
WORKDIR /app

COPY preview-server.js index.html dashboard.js auth.js auth-pages.css ./
COPY login.html login.js security.html security.js ./

USER node
EXPOSE 4173

CMD ["node", "preview-server.js"]
