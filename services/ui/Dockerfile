# The unprivileged nginx image (gap audit I20): same nginx, already built to run as a
# non-root user with writable pid and cache paths, listening on 8080 because a
# non-root process cannot bind port 80.
FROM nginxinc/nginx-unprivileged:1.31-alpine
COPY --chown=101:101 --chmod=0644 services/ui/nginx.conf /etc/nginx/conf.d/default.conf
# Pins the `api` upstream to its IPv4 address before nginx starts: Service Connect also
# advertises an AAAA that this VPC (no IPv6 CIDR) cannot route, and nginx round-robins the
# addresses it cached at start, so ~15% of /api/* calls failed with "Network unreachable".
COPY --chmod=0755 services/ui/05-pin-api-ipv4.sh /docker-entrypoint.d/05-pin-api-ipv4.sh
COPY services/ui/index.html /usr/share/nginx/html/index.html
EXPOSE 8080
