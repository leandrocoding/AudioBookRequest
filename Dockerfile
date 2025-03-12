FROM node:23-alpine3.20

WORKDIR /app

# Install daisyui
COPY package.json package.json
COPY package-lock.json package-lock.json
RUN npm install

# Setup python
FROM python:3.11-alpine AS linux-amd64
WORKDIR /app
RUN apk add --no-cache curl gcompat build-base
RUN curl https://github.com/tailwindlabs/tailwindcss/releases/download/v4.0.6/tailwindcss-linux-x64-musl -L -o /bin/tailwindcss

FROM python:3.11-alpine AS linux-arm64
WORKDIR /app
RUN apk add --no-cache curl gcompat build-base
RUN curl https://github.com/tailwindlabs/tailwindcss/releases/download/v4.0.6/tailwindcss-linux-arm64-musl -L -o /bin/tailwindcss

FROM ${TARGETOS}-${TARGETARCH}${TARGETVARIANT}
RUN chmod +x /bin/tailwindcss

COPY requirements.txt requirements.txt
RUN pip install -r requirements.txt


COPY --from=0 /app/node_modules/ node_modules/

COPY alembic/ alembic/
COPY alembic.ini alembic.ini
COPY styles/ styles/
COPY static/ static/
COPY templates/ templates/
COPY app/ app/

RUN /bin/tailwindcss -i styles/globals.css -o static/globals.css -m

ENV ABR_APP__PORT=8000

CMD alembic upgrade heads && fastapi run --port $ABR_APP__PORT

