# shellcheck shell=bash
# Shared path guards for install.sh / uninstall.sh (bash 3.2 compatible).
#
# mg_check_dir <path> <mode>
#   mode = install : under $HOME, != $HOME, no symlinks, absent / empty / has marker
#   mode = any     : same path checks, contents not checked
#   mode = purge   : same path checks, must be a directory containing the marker
#                    whose resolved real path is strictly under the resolved $HOME
# Every mode refuses control characters (newline, tab ...) and components
# starting with '-'.
# Prints the reason to stderr and returns 1 when the path is refused.

MG_MARKER=".meeting-guard-home"

mg_refuse() {
  echo "refused: $1: $2" >&2
  return 1
}

mg_real_dir() {
  (cd "$1" 2>/dev/null && pwd -P)
}

mg_has_ctrl() {
  # Control characters (newline, tab, DEL ...) would split or confuse later commands.
  case "$1" in *[[:cntrl:]]*) return 0 ;; esac
  return 1
}

mg_check_shape() {
  local p="$1" home="${HOME%/}"
  ! mg_has_ctrl "$HOME" || { mg_refuse "(HOME)" "HOME contains a control character"; return 1; }
  ! mg_has_ctrl "$p" || { mg_refuse "(path)" "contains a control character (newline, tab ...)"; return 1; }
  case "$p" in */-*) mg_refuse "$p" "has a component starting with '-'"; return 1 ;; esac
  case "$HOME" in /*) ;; *) mg_refuse "$p" "HOME is not an absolute path"; return 1 ;; esac
  [ -n "$home" ] || { mg_refuse "$p" "HOME is /"; return 1; }
  case "$p" in /*) ;; *) mg_refuse "$p" "not an absolute path"; return 1 ;; esac
  [ "$p" != "/" ] || { mg_refuse "$p" "is /"; return 1; }
  case "$p/" in */../*|*/./*|*//*) mg_refuse "$p" "contains . / .. / //"; return 1 ;; esac
  [ "$p" != "$home" ] || { mg_refuse "$p" "is \$HOME itself"; return 1; }
  case "$p" in "$home"/*) ;; *) mg_refuse "$p" "is outside \$HOME"; return 1 ;; esac
  [ ! -L "$p" ] || { mg_refuse "$p" "is a symlink"; return 1; }
  return 0
}

mg_check_ancestors() {
  # The nearest existing ancestor must resolve to the same place under the real
  # $HOME (no symlinked component between $HOME and the target).
  local p="$1" home="${HOME%/}" anc real_anc real_home
  anc="$p"
  while [ ! -e "$anc" ] && [ ! -L "$anc" ]; do anc="$(dirname "$anc")"; done
  real_home="$(mg_real_dir "$home")" || { mg_refuse "$p" "cannot resolve \$HOME"; return 1; }
  real_anc="$(mg_real_dir "$anc")" || { mg_refuse "$p" "$anc is not a directory"; return 1; }
  [ "$real_anc" = "$real_home${anc#"$home"}" ] || {
    mg_refuse "$p" "path goes through a symlink"; return 1; }
  return 0
}

mg_check_realpath() {
  # The fully resolved directory must be strictly under the resolved $HOME.
  local p="$1" real real_home
  real_home="$(mg_real_dir "${HOME%/}")" || { mg_refuse "$p" "cannot resolve \$HOME"; return 1; }
  real="$(mg_real_dir "$p")" || { mg_refuse "$p" "cannot resolve the real path"; return 1; }
  ! mg_has_ctrl "$real" || { mg_refuse "(path)" "real path contains a control character"; return 1; }
  [ "$real" != "$real_home" ] || { mg_refuse "$p" "resolves to \$HOME itself"; return 1; }
  case "$real" in "$real_home"/*) ;; *) mg_refuse "$p" "resolves outside \$HOME"; return 1 ;; esac
  return 0
}

mg_has_marker() {
  [ -f "$1/$MG_MARKER" ] && [ ! -L "$1/$MG_MARKER" ]
}

mg_check_dir() {
  local p="${1%/}" mode="$2"
  [ -n "$p" ] || p="/"
  mg_check_shape "$p" || return 1
  mg_check_ancestors "$p" || return 1
  case "$mode" in
    install)
      if [ -d "$p" ] && [ -n "$(ls -A "$p")" ] && ! mg_has_marker "$p"; then
        mg_refuse "$p" "exists, is not empty and was not created by meeting-guard ($MG_MARKER missing)"
        return 1
      fi ;;
    purge)
      [ -d "$p" ] || { mg_refuse "$p" "is not a directory"; return 1; }
      mg_check_realpath "$p" || return 1
      mg_has_marker "$p" || { mg_refuse "$p" "$MG_MARKER missing (not created by install.sh)"; return 1; } ;;
    any) ;;
    *) mg_refuse "$p" "unknown mode $mode"; return 1 ;;
  esac
  return 0
}

mg_write_marker() {
  [ ! -L "$1/$MG_MARKER" ] || { mg_refuse "$1/$MG_MARKER" "is a symlink"; return 1; }
  echo "created by meeting-guard install.sh; uninstall.sh --purge deletes this directory" \
    > "$1/$MG_MARKER"
}
