ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /app
COPY rootfs/app/requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY rootfs/ /

RUN chmod +x /etc/services.d/spm-bridge/run \
    && chmod +x /etc/services.d/spm-bridge/finish \
    && chmod +x /etc/cont-init.d/init.sh
