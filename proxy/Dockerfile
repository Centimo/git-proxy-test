FROM python:3.12-alpine

RUN apk add --no-cache git
RUN pip install --no-cache-dir pyyaml

WORKDIR /app
COPY proxy.py config.py hooks.py .
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
