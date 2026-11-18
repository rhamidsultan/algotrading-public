# Sourced by the Mac launchers (deploy/*.command, the IBKR helpers): which server to reach and with which key.
# Kept on this Mac only, in ~/.algo-trading/server.env, so the code on GitHub holds nobody's server:
#     ALGO_HOST="ubuntu@203.0.113.10"                   # the login and the server's address (Lightsail: Public IP)
#     ALGO_KEY="$HOME/Downloads/LightsailDefaultKey.pem"  # the SSH key file Lightsail gave you
# The first launcher you open asks for both and saves them. Settings already in the environment win (tests use that).
ALGO_SERVER_CONF="${ALGO_SERVER_CONF:-$HOME/.algo-trading/server.env}"
_h="${ALGO_HOST:-}"; _k="${ALGO_KEY:-}"
# shellcheck disable=SC1090
[ -f "$ALGO_SERVER_CONF" ] && . "$ALGO_SERVER_CONF"
[ -n "$_h" ] && ALGO_HOST="$_h"
[ -n "$_k" ] && ALGO_KEY="$_k"
unset _h _k
if [ -z "${ALGO_HOST:-}" ]; then
  if [ ! -t 0 ]; then echo "No server set. Open this launcher in Terminal once to enter it."; exit 1; fi
  echo "Which server? (asked once; saved in $ALGO_SERVER_CONF)"
  read -r -p "  Server address (Lightsail > your instance > Public IP, e.g. 203.0.113.10): " _ip
  _ip="$(echo "$_ip" | tr -d '[:space:]')"
  [ -n "$_ip" ] || { echo "Nothing entered."; exit 1; }
  case "$_ip" in *@*) ALGO_HOST="$_ip" ;; *) ALGO_HOST="ubuntu@$_ip" ;; esac
  if [ -z "${ALGO_KEY:-}" ]; then
    _guess="$(ls "$HOME"/Downloads/*.pem "$HOME"/.ssh/*.pem 2>/dev/null | head -1)"
    read -r -p "  SSH key file [${_guess:-none found}]: " _key
    ALGO_KEY="${_key:-$_guess}"
    ALGO_KEY="${ALGO_KEY/#\~/$HOME}"
  fi
  mkdir -p "$(dirname "$ALGO_SERVER_CONF")" && chmod 700 "$(dirname "$ALGO_SERVER_CONF")"
  printf 'ALGO_HOST="%s"\nALGO_KEY="%s"\n' "$ALGO_HOST" "$ALGO_KEY" > "$ALGO_SERVER_CONF"
  chmod 600 "$ALGO_SERVER_CONF"
  echo "  Saved. To change it later, edit or delete $ALGO_SERVER_CONF"
  unset _ip _guess _key
fi
ALGO_KEY="${ALGO_KEY:-}"
ALGO_SERVER_ADDR="${ALGO_HOST#*@}"    # the address alone (for ssh-keygen -R)
