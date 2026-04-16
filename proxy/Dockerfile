FROM python:3.12-alpine

RUN apk add --no-cache git
RUN pip install --no-cache-dir pyyaml

WORKDIR /app
COPY proxy/proxy.py proxy/config.py proxy/hooks.py .
COPY proxy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
