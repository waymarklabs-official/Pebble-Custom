#!/usr/bin/env bash
# Make the keys the Tesla integration needs, in data/tesla/. Never replaces an existing key:
# re-running it is safe, and your car keeps trusting the hub.
#
#   private-key.pem  signs your car commands (prime256v1). Stays on this machine; the proxy reads it.
#   public-key.pem   host it at https://<TESLA_DOMAIN>/.well-known/appspecific/com.tesla.3p.public-key.pem
#   tls-key.pem, tls-cert.pem   TLS for the proxy, which only ringhub talks to (as https://tesla-proxy:4443)
#
#   bash deploy/pi/tesla-keys.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
command -v openssl >/dev/null || { echo "openssl is needed: sudo apt install openssl" >&2; exit 2; }
DIR=data/tesla
mkdir -p "$DIR"
umask 077

if [ -s "$DIR/private-key.pem" ]; then
  echo "keeping the existing private key ($DIR/private-key.pem)"
else
  openssl ecparam -name prime256v1 -genkey -noout -out "$DIR/private-key.pem"
  echo "made a new private key ($DIR/private-key.pem): back it up with the rest of data/"
fi
# Derived from the private key, so rewriting it never changes it.
openssl ec -in "$DIR/private-key.pem" -pubout -out "$DIR/public-key.pem" 2>/dev/null

if [ -s "$DIR/tls-cert.pem" ] && [ -s "$DIR/tls-key.pem" ]; then
  echo "keeping the existing proxy TLS certificate"
else
  # A one-certificate CA for the name tesla-proxy: ringhub trusts exactly this certificate.
  openssl req -x509 -nodes -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -sha256 -days 3650 \
    -subj '/CN=tesla-proxy' -addext 'subjectAltName=DNS:tesla-proxy' \
    -addext 'keyUsage=critical,digitalSignature,keyCertSign' -addext 'extendedKeyUsage=serverAuth' \
    -keyout "$DIR/tls-key.pem" -out "$DIR/tls-cert.pem" 2>/dev/null
  echo "made the proxy's TLS certificate ($DIR/tls-cert.pem)"
fi
chmod 600 "$DIR/private-key.pem" "$DIR/tls-key.pem"
chmod 644 "$DIR/public-key.pem" "$DIR/tls-cert.pem"
chmod 700 "$DIR"
# The containers run as uid 1000.
if [ "$(id -u)" = 0 ]; then chown -R 1000:1000 "$DIR"; fi

echo
echo "Public key (host this file at https://<TESLA_DOMAIN>/.well-known/appspecific/com.tesla.3p.public-key.pem):"
cat "$DIR/public-key.pem"
