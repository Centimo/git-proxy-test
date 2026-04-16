FROM codeberg.org/forgejo/forgejo:9

USER root

RUN apk add --no-cache curl bash python3 py3-yaml

COPY forgejo/app.ini /etc/forgejo/app.ini
COPY forgejo/entrypoint.sh /entrypoint.sh
COPY proxy/proxy.py proxy/config.py proxy/hooks.py /app/
RUN chmod +x /entrypoint.sh

VOLUME ["/data"]
EXPOSE 3000 8080

ENTRYPOINT ["/entrypoint.sh"]
