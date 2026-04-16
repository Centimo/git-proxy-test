FROM python:3.12-alpine AS python-bin
RUN pip install --no-cache-dir pyyaml

FROM codeberg.org/forgejo/forgejo:9

USER root

RUN apk add --no-cache curl bash

# Copy Python runtime from official image
COPY --from=python-bin /usr/local/bin/python3 /usr/local/bin/python3
COPY --from=python-bin /usr/local/lib/python3.12 /usr/local/lib/python3.12
COPY --from=python-bin /usr/local/lib/libpython3.12.so.1.0 /usr/local/lib/libpython3.12.so.1.0

COPY forgejo/app.ini /etc/forgejo/app.ini
COPY forgejo/entrypoint.sh /entrypoint.sh
COPY proxy/proxy.py proxy/config.py proxy/hooks.py /app/
RUN chmod +x /entrypoint.sh

VOLUME ["/data"]
EXPOSE 3000 8080

ENTRYPOINT ["/entrypoint.sh"]
