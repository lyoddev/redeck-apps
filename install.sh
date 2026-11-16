#!/usr/bin/env bash
# Установщик программ ReDeck (рассчитан на Pop!_OS 22.04).
# Ставит программу из папки apps/<ID>: системные пакеты, свои библиотеки
# Python в отдельной папке venv, команду запуска и ярлык в меню.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$HOME/.local/share/redeck/apps"
BIN="$HOME/.local/bin"
DESK="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"

usage() {
  echo "Использование:"
  echo "  ./install.sh список         показать доступные программы"
  echo "  ./install.sh ID             установить программу"
  echo "  ./install.sh удалить ID     удалить программу"
  echo "  ./install.sh ярлык ID       обновить только ярлык и название в меню"
}

list_apps() {
  echo "Доступные программы:"
  local c
  for c in "$HERE"/apps/*/app.conf; do
    [ -e "$c" ] || continue
    ( source "$c"; echo "  $ID  $NAME" )
  done
}

remove_app() {
  local id="$1" dname="$1"
  local conf="$HERE/apps/$id/app.conf"
  if [ -f "$conf" ]; then
    dname="$( source "$conf"; echo "${DESKTOP_ID:-$id}" )"
  fi
  rm -rf "$ROOT/$id" "$BIN/$id" "$DESK/$dname.desktop" "$DESK/redeck-$id.desktop" "$ICONS/$dname.svg"
  echo "Удалено: $id"
}

write_shortcut() {
  local id="$1"
  local dname="${DESKTOP_ID:-$id}"
  local icon_name="${ICON:-applications-other}"
  if [ -n "${ICON_FILE:-}" ] && [ -f "$HERE/apps/$id/$ICON_FILE" ]; then
    mkdir -p "$ICONS"
    cp "$HERE/apps/$id/$ICON_FILE" "$ICONS/$dname.svg"
    icon_name="$dname"
  fi

  rm -f "$DESK/redeck-$id.desktop"
  cat > "$DESK/$dname.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=$NAME
Comment=${COMMENT:-}
Exec=$BIN/$id
Icon=$icon_name
Terminal=false
Categories=${CATEGORY:-Utility;}
${WMCLASS:+StartupWMClass=$WMCLASS}
EOF
  gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
  update-desktop-database "$DESK" 2>/dev/null || true
}

relink_app() {
  local id="$1"
  local conf="$HERE/apps/$id/app.conf"
  if [ ! -f "$conf" ]; then
    echo "Нет такой программы: $id"
    list_apps
    exit 1
  fi
  # shellcheck disable=SC1090
  source "$conf"
  if [ ! -d "$ROOT/$id" ]; then
    echo "Программа $id не установлена. Сначала: bash install.sh $id"
    exit 1
  fi
  write_shortcut "$id"
  echo "Ярлык обновлён: $NAME"
}

install_app() {
  local id="$1"
  local conf="$HERE/apps/$id/app.conf"
  if [ ! -f "$conf" ]; then
    echo "Нет такой программы: $id"
    list_apps
    exit 1
  fi
  # shellcheck disable=SC1090
  source "$conf"

  if [ -n "${USE_ENV_OF:-}" ] && [ ! -x "$ROOT/$USE_ENV_OF/venv/bin/python" ]; then
    echo "Сначала установи программу $USE_ENV_OF: эта использует её библиотеки."
    echo "Команда: bash install.sh $USE_ENV_OF"
    exit 1
  fi
  if [ "${NEEDS_X11:-0}" = "1" ] && [ "${XDG_SESSION_TYPE:-x11}" != "x11" ]; then
    echo "ВНИМАНИЕ: программа рассчитана на сессию X11, а у тебя ${XDG_SESSION_TYPE}. Часть функций может не работать."
  fi

  echo "==> Системные пакеты"
  local need=(python3-venv python3-pip) pkg
  for pkg in ${APT:-}; do need+=("$pkg"); done
  local missing=()
  for pkg in "${need[@]}"; do
    dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    echo "Нужно доустановить: ${missing[*]} (попросит пароль)"
    sudo apt-get update
    sudo apt-get install -y "${missing[@]}"
  else
    echo "Всё уже установлено."
  fi

  echo "==> Копирую файлы программы"
  mkdir -p "$ROOT/$id" "$BIN" "$DESK"
  cp -r "$HERE/apps/$id/." "$ROOT/$id/"
  rm -rf "$ROOT/$id/venv"

  local py="$ROOT/$id/venv/bin/python"
  if [ -n "${USE_ENV_OF:-}" ]; then
    echo "==> Использую библиотеки программы $USE_ENV_OF, свои не ставлю"
    py="$ROOT/$USE_ENV_OF/venv/bin/python"
  elif [ "${PYMODE:-venv}" = "system" ]; then
    echo "==> Эта программа работает на системном Python, отдельная папка не нужна"
    py="/usr/bin/python3"
  else
    echo "==> Ставлю библиотеки Python в отдельную папку"
    local venv_flags=""
    if [ "${SYSTEM_SITE:-0}" = "1" ]; then venv_flags="--system-site-packages"; fi
    # shellcheck disable=SC2086
    python3 -m venv $venv_flags "$ROOT/$id/venv"
    "$ROOT/$id/venv/bin/pip" install --upgrade pip
    if [ -f "$ROOT/$id/requirements.txt" ]; then
      "$ROOT/$id/venv/bin/pip" install -r "$ROOT/$id/requirements.txt"
    fi
    if [ -n "${PIP:-}" ]; then
      # shellcheck disable=SC2086
      "$ROOT/$id/venv/bin/pip" install $PIP
    fi
  fi

  echo "==> Создаю команду запуска и ярлык"
  if [ -n "${RUN_MODULE:-}" ]; then
    cat > "$BIN/$id" <<EOF
#!/usr/bin/env bash
cd "$ROOT/$id" && exec "$py" -m $RUN_MODULE "\$@"
EOF
  else
    cat > "$BIN/$id" <<EOF
#!/usr/bin/env bash
exec "$py" "$ROOT/$id/$MAIN" "\$@"
EOF
  fi
  chmod +x "$BIN/$id"

  write_shortcut "$id"

  echo
  echo "Готово: $NAME"
  echo "Ищи в меню приложений или запускай командой: $id"
  case ":$PATH:" in
    *":$BIN:"*) ;;
    *) echo "Подсказка: добавь $BIN в PATH, чтобы запускать из терминала." ;;
  esac
}

case "${1:-}" in
  ""|-h|--help) usage; echo; list_apps ;;
  список|list)  list_apps ;;
  удалить|remove) remove_app "${2:?Укажи ID программы}" ;;
  ярлык|relink) relink_app "${2:?Укажи ID программы}" ;;
  *) install_app "$1" ;;
esac
