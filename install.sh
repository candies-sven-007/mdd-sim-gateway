#!/bin/sh
# MDD Sim Gateway (slim, systemd) installer for Debian 11 arm64 devices.
#
# Run from an extracted release (build/build-arm64.sh); it needs no GitHub, PyPI or npm access.
# apt is used for system libraries only, from whatever sources the device already has.
#
#   sudo ./install.sh install [--xray PATH]   install or upgrade from ./payload
#   sudo ./install.sh status                  services, lines and network state
#   sudo ./install.sh logs [control|orchestrator|pcscd|engine N] [LINES]
#   sudo ./install.sh reset-admin             forget the administrator; set it up again in the web UI
#   sudo ./install.sh uninstall [--purge]     remove; --purge also deletes /var/lib/mdd
#
# What it changes is listed in docs/SLIM_PLAN.md section 7 and undone by `uninstall`.
set -eu

PREFIX_ROOT=/opt/mdd
CURRENT="$PREFIX_ROOT/current"
DATA=/var/lib/mdd
STATE="$DATA/install-state"
UNIT_DIR=/etc/systemd/system
UNITS="mdd-pcscd.service mdd-orchestrator.service mdd-control.service mdd-netns@.service mdd-engine@.service"
SERVICES="mdd-pcscd.service mdd-orchestrator.service mdd-control.service"
ENGINECTL=/usr/local/sbin/mdd-enginectl
BTCTL=/usr/local/sbin/mdd-btctl
SUDOERS=/etc/sudoers.d/mdd
MODULES_FILE=/etc/modules-load.d/mdd.conf
SYSCTL_FILE=/etc/sysctl.d/90-mdd.conf
ZRAM_UNIT=mdd-zram.service
ZRAM_SCRIPT=/usr/local/sbin/mdd-zram
KERNEL_MODULES="tun veth nf_tables nft_nat nft_chain_nat nft_ct nft_reject_inet nft_masq"
REQUIRED_MODULES="tun veth nf_tables"
# Each engine unit gives Asterisk a private, empty directory over these (TemporaryFileSystem=).
MOUNT_POINTS="/etc/asterisk /var/lib/asterisk /var/spool/asterisk"
MIN_SYSTEMD=247

SELF_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CMD=${1:-help}
[ $# -gt 0 ] && shift

if [ -t 1 ]; then B=$(printf '\033[1m'); G=$(printf '\033[32m'); Y=$(printf '\033[33m'); R=$(printf '\033[31m'); N=$(printf '\033[0m'); else B=; G=; Y=; R=; N=; fi
info() { printf '%s==>%s %s\n' "$G$B" "$N" "$*"; }
warn() { printf '%s!!%s %s\n' "$Y$B" "$N" "$*"; }
die()  { printf '%sxx%s %s\n' "$R$B" "$N" "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
need_root() { [ "$(id -u)" -eq 0 ] || die "run as root: sudo $0 $CMD"; }

state_set() {   # key value
  mkdir -p "$DATA"
  touch "$STATE"
  grep -v "^$1=" "$STATE" > "$STATE.tmp" 2>/dev/null || true
  printf '%s=%s\n' "$1" "$2" >> "$STATE.tmp"
  mv "$STATE.tmp" "$STATE"
  chmod 0600 "$STATE"
}

state_get() { [ -f "$STATE" ] && sed -n "s/^$1=//p" "$STATE" | tail -n1 || true; }

lan_address() {
  ip -4 -o route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n1
}

control_port() {
  port=$(sed -n 's/^[[:space:]]*http_port:[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$DATA/config.yaml" 2>/dev/null | head -n1)
  echo "${port:-8443}"
}

# ------------------------------------------------------------------------------------ checks
preflight() {
  arch=$(uname -m)
  [ "$arch" = aarch64 ] || die "this release is for aarch64 (arm64); this machine is $arch"
  have systemctl || die "systemd is required"
  systemd_version=$(systemctl --version | sed -n '1s/^systemd \([0-9]*\).*/\1/p')
  [ "${systemd_version:-0}" -ge "$MIN_SYSTEMD" ] || die "systemd $MIN_SYSTEMD or newer is required (found ${systemd_version:-unknown})"
  have ip || die "iproute2 is required (apt install iproute2)"
  for module in $KERNEL_MODULES; do modprobe "$module" 2>/dev/null || true; done
  for module in $REQUIRED_MODULES; do
    modprobe "$module" 2>/dev/null || grep -qw "^$module" /proc/modules 2>/dev/null || \
      [ -d "/sys/module/$module" ] || die "kernel module $module is not available"
  done
  [ -c /dev/net/tun ] || die "/dev/net/tun is missing"
  if ip netns add mdd-probe 2>/dev/null; then
    ip netns del mdd-probe
  else
    die "network namespaces are not available (ip netns add failed)"
  fi
  # The lines use 10.88.0.0/16; another interface in it would make them ambiguous.
  clash=$(ip -4 -o addr show | awk '$2 !~ /^mdd/ && $4 ~ /^10\.88\./ {print $2" "$4}' | head -n1)
  [ -z "$clash" ] || die "$clash overlaps 10.88.0.0/16, which the lines use"
  if systemctl list-unit-files 'mdd-sim-gateway-*' 2>/dev/null | grep -q mdd-sim-gateway-; then
    die "an older (Docker-based) MDD Sim Gateway is installed; remove it first"
  fi
}

verify_release() {
  [ -d "$SELF_DIR/payload" ] || die "run this from an extracted release (payload/ not found next to $0)"
  [ -f "$SELF_DIR/SHA256SUMS" ] || die "SHA256SUMS missing from the release"
  info "verifying release files"
  (cd "$SELF_DIR" && sha256sum -c --quiet SHA256SUMS) || die "release files do not match SHA256SUMS"
  VERSION=$(tr -d ' \n' < "$SELF_DIR/payload/VERSION")
  [ -n "$VERSION" ] || die "release has no VERSION"
}

# ------------------------------------------------------------------------------------ install
install_packages() {
  packages=$(grep -v '^[[:space:]]*\(#\|$\)' "$SELF_DIR/payload/apt-packages.txt" | tr '\n' ' ')
  missing=""
  for package in $packages; do
    dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q 'ok installed' || missing="$missing $package"
  done
  if [ -n "$missing" ]; then
    info "installing system packages:$missing"
    apt-get update -q || warn "apt-get update failed; trying with the current package lists"
    # shellcheck disable=SC2086
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends $missing || die \
      "apt could not install:$missing
   Debian 11 left LTS on 2026-08-31; if its sources no longer answer, point
   /etc/apt/sources.list at a mirror that still carries bullseye (this installer does not change it)."
  fi
}

replace_distro_pcscd() {
  # pcscd from the release owns the readers (D8): the distribution's daemon and driver go.
  for unit in pcscd.socket pcscd.service; do
    if systemctl list-unit-files "$unit" 2>/dev/null | grep -q "^$unit"; then
      systemctl stop "$unit" 2>/dev/null || true
      systemctl mask "$unit" >/dev/null 2>&1 || true
      state_set masked_pcscd 1
    fi
  done
  removed=""
  for package in pcscd libccid; do
    if dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q 'ok installed'; then
      removed="$removed $package"
    fi
  done
  if [ -n "$removed" ]; then
    info "removing the distribution's$removed (restored by uninstall)"
    # shellcheck disable=SC2086
    DEBIAN_FRONTEND=noninteractive apt-get remove -y -q $removed
    state_set removed_packages "$(echo $removed)"
  fi
}

configure_kernel() {
  printf '# MDD Sim Gateway: line namespaces, TUN exits and nftables (install.sh)\n' > "$MODULES_FILE"
  for module in $KERNEL_MODULES; do printf '%s\n' "$module" >> "$MODULES_FILE"; done
  printf '# MDD Sim Gateway: lines reach their country TUN through the host\nnet.ipv4.ip_forward = 1\n' > "$SYSCTL_FILE"
  sysctl -q -p "$SYSCTL_FILE" >/dev/null
}

create_user() {
  if ! getent passwd mdd >/dev/null; then
    info "creating system user mdd"
    useradd --system --user-group --home-dir "$DATA" --no-create-home --shell /usr/sbin/nologin mdd
  fi
  usermod -a -G systemd-journal mdd
  install -d -m 0750 -o mdd -g mdd "$DATA"
  if [ -d "$DATA/orchestrator" ]; then chown mdd:mdd "$DATA/orchestrator"; fi
  for point in $MOUNT_POINTS; do
    if [ ! -d "$point" ]; then
      install -d -m 0755 "$point"
      state_set "created:$point" 1
    fi
  done
}

install_payload() {
  destination="$PREFIX_ROOT/$VERSION"
  info "installing $VERSION into $destination"
  mkdir -p "$PREFIX_ROOT"
  rm -rf "${PREFIX_ROOT:?}/${VERSION:?}.new"
  cp -a "$SELF_DIR/payload" "$destination.new"
  chown -R root:root "$destination.new"
  chmod -R go-w "$destination.new"
  if [ -n "${XRAY_ARCHIVE:-}" ]; then
    info "adding Xray from $XRAY_ARCHIVE"
    tar -xzf "$XRAY_ARCHIVE" -C "$destination.new" ./bin/xray
    chmod 0755 "$destination.new/bin/xray"
  elif [ -x "$CURRENT/bin/xray" ] && [ ! -e "$destination.new/bin/xray" ]; then
    cp -a "$CURRENT/bin/xray" "$destination.new/bin/xray"      # keep it across upgrades
  fi
  rm -rf "${PREFIX_ROOT:?}/${VERSION:?}"
  mv "$destination.new" "$destination"
  ln -sfn "$VERSION" "$PREFIX_ROOT/current.new"
  mv -T "$PREFIX_ROOT/current.new" "$CURRENT"
}

install_units() {
  for unit in $UNITS; do
    install -m 0644 "$CURRENT/packaging/systemd/$unit" "$UNIT_DIR/$unit"
  done
  install -m 0755 -o root -g root "$CURRENT/packaging/sbin/mdd-enginectl" "$ENGINECTL"
  install -m 0755 -o root -g root "$CURRENT/packaging/sbin/mdd-btctl" "$BTCTL"
  temporary=$(mktemp)
  cp "$CURRENT/packaging/sudoers.d/mdd" "$temporary"
  visudo -cf "$temporary" >/dev/null || { rm -f "$temporary"; die "sudoers fragment failed visudo -cf"; }
  install -m 0440 -o root -g root "$temporary" "$SUDOERS"
  rm -f "$temporary"
}

ensure_zram() {
  if grep -q '^/dev/zram' /proc/swaps 2>/dev/null; then
    info "zram swap already active; leaving it"
    return
  fi
  modprobe zram 2>/dev/null || { warn "no zram module; continuing without compressed swap"; return; }
  cat > "$ZRAM_SCRIPT" <<'EOF'
#!/bin/sh
# Compressed swap of half the RAM, created by MDD Sim Gateway install.sh.
set -eu
if [ "${1:-start}" = stop ]; then
  dev=$(cat /run/mdd-zram 2>/dev/null) || exit 0
  swapoff "$dev" || true
  zramctl --reset "$dev" || true
  exec rm -f /run/mdd-zram
fi
modprobe zram
kib=$(awk '/MemTotal/ {print int($2 / 2)}' /proc/meminfo)
dev=$(zramctl --find --size "${kib}KiB" --algorithm lzo-rle 2>/dev/null || zramctl --find --size "${kib}KiB")
mkswap "$dev" >/dev/null
swapon -p 100 "$dev"
echo "$dev" > /run/mdd-zram
EOF
  chmod 0755 "$ZRAM_SCRIPT"
  cat > "$UNIT_DIR/$ZRAM_UNIT" <<EOF
[Unit]
Description=MDD Sim Gateway compressed swap (zram, half of RAM)

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$ZRAM_SCRIPT start
ExecStop=$ZRAM_SCRIPT stop

[Install]
WantedBy=multi-user.target
EOF
  state_set zram_unit 1
}

check_time() {
  if have timedatectl && [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" != yes ]; then
    warn "the clock is not NTP-synchronised; IKE and TLS need correct time (e.g. ntp.aliyun.com)"
  fi
}

cmd_install() {
  need_root
  XRAY_ARCHIVE=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --xray) [ $# -ge 2 ] || die "--xray needs a path"; XRAY_ARCHIVE=$(readlink -f "$2"); shift 2 ;;
      *) die "unknown option: $1" ;;
    esac
  done
  if [ -z "$XRAY_ARCHIVE" ]; then
    for candidate in "$SELF_DIR"/../mdd-slim-xray-arm64-*.tar.gz; do
      [ -f "$candidate" ] && XRAY_ARCHIVE=$(readlink -f "$candidate")
    done
  fi
  verify_release
  preflight
  upgrade=0; [ -L "$CURRENT" ] && upgrade=1
  install_packages
  have nft || die "nftables did not install"
  replace_distro_pcscd
  configure_kernel
  create_user
  install_payload
  install_units
  ensure_zram
  systemctl daemon-reload
  [ -f "$UNIT_DIR/$ZRAM_UNIT" ] && systemctl enable --now "$ZRAM_UNIT" >/dev/null 2>&1 || true
  # shellcheck disable=SC2086
  systemctl enable $SERVICES >/dev/null
  if [ "$upgrade" = 1 ]; then
    info "restarting services on the new release"
    # shellcheck disable=SC2086
    systemctl restart $SERVICES
    systemctl try-restart 'mdd-engine@*.service' 2>/dev/null || true
  else
    # shellcheck disable=SC2086
    systemctl start $SERVICES
  fi
  # Older releases (the symlink `current` is not a directory, so it stays).
  find "${PREFIX_ROOT:?}" -mindepth 1 -maxdepth 1 -type d ! -name "${VERSION:?}" -exec rm -rf {} + 2>/dev/null || true
  check_time
  address=$(lan_address)
  info "MDD Sim Gateway $VERSION installed"
  printf '   Open https://%s:%s and set up the administrator.\n' "${address:-<device address>}" "$(control_port)"
  printf '   Status: sudo %s status    Logs: sudo %s logs control\n' "$0" "$0"
}

# ------------------------------------------------------------------------------------- status
cmd_status() {
  printf 'release   %s\n' "$(readlink "$CURRENT" 2>/dev/null || echo 'not installed')"
  for unit in $SERVICES; do
    printf '%-24s %s\n' "$unit" "$(systemctl is-active "$unit" 2>/dev/null || true)"
  done
  printf '\nlines:\n'
  systemctl list-units --all --no-legend --plain 'mdd-engine@*.service' 'mdd-netns@*.service' 2>/dev/null | \
    awk '{printf "  %-26s %s/%s\n", $1, $3, $4}'
  printf '\nnamespaces: %s\n' "$(ip netns list 2>/dev/null | awk '{print $1}' | grep '^mdd-' | tr '\n' ' ')"
  if have nft; then
    printf 'nftables:   %s\n' "$(nft list tables 2>/dev/null | grep -E 'mdd' | tr '\n' ' ')"
  fi
  if [ -x "$CURRENT/bin/xray" ]; then printf 'xray:       installed\n'; else printf 'xray:       not installed (only needed for REALITY/XHTTP nodes)\n'; fi
  printf 'web UI:     https://%s:%s\n' "$(lan_address)" "$(control_port)"
}

cmd_logs() {
  component=${1:-control}
  case "$component" in
    control|orchestrator|pcscd) unit="mdd-$component.service"; lines=${2:-200} ;;
    engine)
      line=${2:-}
      case "$line" in [1-9]|10) ;; *) die "usage: $0 logs engine <line 1-10> [LINES]" ;; esac
      unit="mdd-engine@$line.service"; lines=${3:-200} ;;
    *) die "usage: $0 logs [control|orchestrator|pcscd|engine N] [LINES]" ;;
  esac
  case "$lines" in *[!0-9]*|'') die "LINES must be a number" ;; esac
  journalctl -u "$unit" -n "$lines" --no-pager
}

cmd_reset_admin() {
  need_root
  rm -f "${DATA:?}/auth.json" "${DATA:?}/sessions.json"
  info "administrator removed; open the web UI to set a new one"
}

# ---------------------------------------------------------------------------------- uninstall
cmd_uninstall() {
  need_root
  purge=0
  [ "${1:-}" = --purge ] && purge=1
  info "stopping services and lines"
  systemctl stop 'mdd-engine@*.service' 'mdd-netns@*.service' 2>/dev/null || true
  # shellcheck disable=SC2086
  systemctl disable --now $SERVICES 2>/dev/null || true
  if [ "$(state_get zram_unit)" = 1 ]; then
    systemctl disable --now "$ZRAM_UNIT" 2>/dev/null || true
    rm -f "${UNIT_DIR:?}/${ZRAM_UNIT:?}" "${ZRAM_SCRIPT:?}"
  fi
  for unit in $UNITS; do rm -f "${UNIT_DIR:?}/${unit:?}"; done
  systemctl daemon-reload
  if have nft; then
    nft delete table inet mdd 2>/dev/null || true
    nft delete table ip mdd_nat 2>/dev/null || true
  fi
  rm -f "${SUDOERS:?}" "${ENGINECTL:?}" "${BTCTL:?}" "${MODULES_FILE:?}" "${SYSCTL_FILE:?}"
  for point in $MOUNT_POINTS; do
    if [ "$(state_get "created:$point")" = 1 ]; then rmdir "$point" 2>/dev/null || true; fi
  done
  if [ "$(state_get masked_pcscd)" = 1 ]; then
    systemctl unmask pcscd.socket pcscd.service >/dev/null 2>&1 || true
  fi
  removed=$(state_get removed_packages)
  if [ -n "$removed" ]; then
    info "reinstalling the distribution's $removed"
    # shellcheck disable=SC2086
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q $removed || \
      warn "could not reinstall $removed; install them again with apt when the sources answer"
  fi
  rm -rf "${PREFIX_ROOT:?}"
  if [ "$purge" = 1 ]; then
    rm -rf "${DATA:?}"
    getent passwd mdd >/dev/null && userdel mdd 2>/dev/null || true
    info "removed, including $DATA"
  else
    rm -f "${STATE:?}"
    info "removed; settings and history are kept in $DATA (uninstall --purge deletes them)"
  fi
}

case "$CMD" in
  install)     cmd_install "$@" ;;
  status)      cmd_status ;;
  logs)        cmd_logs "$@" ;;
  reset-admin) cmd_reset_admin ;;
  uninstall)   cmd_uninstall "$@" ;;
  help|-h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//' ;;
  *) die "unknown command: $CMD (install, status, logs, reset-admin, uninstall)" ;;
esac
