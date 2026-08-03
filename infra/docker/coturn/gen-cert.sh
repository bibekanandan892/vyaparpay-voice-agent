#!/usr/bin/env sh
#
# infra/docker/coturn/gen-cert.sh — generate the self-signed cert/key that
# coturn's `turns:` listener on 5349 needs (turnserver.conf `cert=`/`pkey=`).
#
# ============================== DEMO ONLY ==============================
# This produces a SELF-SIGNED certificate. Nothing outside your own machine
# should ever trust it:
#
#   * Android's WebRTC stack will reject a `turns:` URL whose cert does not
#     chain to a trusted CA, so on a real phone the relay falls back to
#     plain `turn:` on 3478 unless you install this cert on the device or
#     replace it with a real one.
#   * The name in the cert must match the host in the minted `turns:` URL
#     (COTURN_HOST in backend/.env), or the handshake fails on hostname
#     verification even after the chain is trusted.
#
# A real deployment replaces both files with a CA-issued cert (Let's
# Encrypt, or whatever your gateway already terminates) for the real
# COTURN_HOST name, and rotates them on the CA's schedule. This script
# exists so `docker compose up` works on a laptop, not so 5349 is secure.
# =======================================================================
#
# Idempotent: if both files already exist it prints and exits 0, so it is
# safe to call from a Makefile, a setup script, or twice by hand. Delete
# the certs/ directory to force a regeneration (e.g. after the 365-day
# expiry, or to re-issue for a different host).
#
# Usage:
#   ./infra/docker/coturn/gen-cert.sh                 # CN/SAN = localhost
#   TURN_CERT_HOST=192.168.1.42 ./gen-cert.sh         # CN/SAN = that LAN IP
#   TURN_CERT_HOST=turn.example.com ./gen-cert.sh     # CN/SAN = that name
#
# TURN_CERT_HOST should be whatever you put in COTURN_HOST in backend/.env
# — that is the name the phone dials and therefore the name that has to be
# in the certificate.

set -eu

# Git Bash / MSYS on Windows rewrites any argument that starts with a slash
# into a Windows path, which turns openssl's `-subj /CN=host` into
# `-subj C:/Program Files/Git/CN=host` and fails the run. This excludes
# exactly that one argument shape from the rewrite. Note what it is NOT:
# the blunt `MSYS_NO_PATHCONV=1`, which also stops the *wanted* conversion
# and leaves openssl.exe holding a `/c/Users/...` path it cannot open.
# Meaningless on Linux and macOS, where exporting it costs nothing.
export MSYS2_ARG_CONV_EXCL='/CN='

# Resolve paths from the script's own location, not the caller's cwd, so
# this works from the repo root, from infra/, or from a Makefile.
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CERT_DIR="${SCRIPT_DIR}/certs"

# Filenames match coturn's own upstream examples and the `cert=`/`pkey=`
# lines in turnserver.conf. Changing them here means changing them there.
CERT_FILE="${CERT_DIR}/turn_server_cert.pem"
KEY_FILE="${CERT_DIR}/turn_server_pkey.pem"

HOST="${TURN_CERT_HOST:-localhost}"
DAYS="${TURN_CERT_DAYS:-365}"

if [ -f "${CERT_FILE}" ] && [ -f "${KEY_FILE}" ]; then
  echo "gen-cert: ${CERT_FILE} and ${KEY_FILE} already exist — nothing to do."
  echo "gen-cert: delete ${CERT_DIR} and re-run to regenerate."
  exit 0
fi

if ! command -v openssl >/dev/null 2>&1; then
  echo "gen-cert: openssl not found on PATH." >&2
  echo "gen-cert: install it (apt install openssl / brew install openssl /" >&2
  echo "gen-cert: it ships with Git for Windows' Git Bash) and re-run." >&2
  exit 1
fi

mkdir -p "${CERT_DIR}"

# A bare IP in a certificate has to be a SAN of type IP, not DNS, or every
# client rejects it. Crude but sufficient test: anything that is only
# digits and dots is an IPv4 literal.
case "${HOST}" in
  *[!0-9.]*) SAN="DNS:${HOST}" ;;
  *)         SAN="IP:${HOST}" ;;
esac
# localhost/127.0.0.1 always ride along so a host-local `turns:` smoke test
# works regardless of what TURN_CERT_HOST is — skipping whichever of the
# two HOST already is, since a duplicated SAN entry is just noise.
case "${HOST}" in
  localhost) SAN="${SAN},IP:127.0.0.1" ;;
  127.0.0.1) SAN="${SAN},DNS:localhost" ;;
  *)         SAN="${SAN},DNS:localhost,IP:127.0.0.1" ;;
esac

echo "gen-cert: issuing a ${DAYS}-day self-signed cert for ${HOST} (${SAN})"

# openssl writes the key before it can fail on the cert, so a failed run
# otherwise leaves a half-generated pair behind that looks like a valid
# one to a careless eye. Clear the trap only once both files are written.
trap 'rm -f "${CERT_FILE}" "${KEY_FILE}"' EXIT

openssl req -x509 -nodes \
  -newkey rsa:2048 \
  -days "${DAYS}" \
  -keyout "${KEY_FILE}" \
  -out "${CERT_FILE}" \
  -subj "/CN=${HOST}" \
  -addext "subjectAltName=${SAN}" \
  -addext "basicConstraints=critical,CA:FALSE" \
  -addext "keyUsage=critical,digitalSignature,keyEncipherment" \
  -addext "extendedKeyUsage=serverAuth"

trap - EXIT

# 0644, including on the private key, and that is deliberate: the
# coturn/coturn image runs as `nobody:nogroup`, the files are bind-mounted
# from the host with the host user's ownership, and a 0600 key would be
# unreadable to the container user — coturn would start with a broken TLS
# listener. This is only tolerable because the key is a throwaway
# self-signed dev key that guards nothing; the same permission on a real
# key would be a finding. Real deployments mount a CA-issued key via a
# secrets mechanism with an owner the container can actually read.
chmod 0644 "${CERT_FILE}" "${KEY_FILE}"

echo "gen-cert: wrote ${CERT_FILE}"
echo "gen-cert: wrote ${KEY_FILE}"
echo "gen-cert: both are gitignored (*.pem) — they must never be committed."
