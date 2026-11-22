#!/bin/sh
# Compile chaque po/<langue>.po en src/optima_air/locale/<langue>/LC_MESSAGES/optima-air.mo,
# là où l'application les cherche quand elle tourne depuis le dépôt. Les .mo ne sont pas
# versionnés. Sans eux, l'application tourne en anglais.
set -eu
depot=$(dirname "$(dirname "$(readlink -f "$0")")")
cd "$depot"
for po in po/*.po; do
    [ -e "$po" ] || continue
    langue=$(basename "$po" .po)
    cible="src/optima_air/locale/$langue/LC_MESSAGES"
    mkdir -p "$cible"
    msgfmt --check --output-file="$cible/optima-air.mo" "$po"
    echo "compiled: $cible/optima-air.mo"
done
