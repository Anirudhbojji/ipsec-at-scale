FROM alpine:3.20
RUN apk add --no-cache strongswan iproute2 iptables tcpdump iperf3 python3 py3-pip bash procps coreutils iputils-ping \
    && pip install --break-system-packages --no-cache-dir vici==5.9.11
COPY entrypoint.sh /entrypoint.sh
COPY telemetry.py /telemetry.py
COPY strongswan.conf /etc/strongswan.conf
RUN chmod +x /entrypoint.sh && mkdir -p /var/log /data
ENTRYPOINT ["/entrypoint.sh"]
