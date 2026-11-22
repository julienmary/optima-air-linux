#!/bin/sh
# Extrait les textes à traduire du code (po/optima-air.pot) et met à jour chaque traduction
# po/<langue>.po. Les textes nouveaux arrivent vides, à traduire à la main.
set -eu
depot=$(dirname "$(dirname "$(readlink -f "$0")")")
cd "$depot"
find src/optima_air -name '*.py' | sort | xargs xgettext \
    --language=Python --from-code=UTF-8 --keyword=_ --keyword=ngettext:1,2 \
    --add-location=file --package-name=optima-air --msgid-bugs-address=https://github.com/julienmary/optima-air-linux/issues \
    --output=po/optima-air.pot
for po in po/*.po; do
    [ -e "$po" ] || continue
    msgmerge --quiet --update --backup=none --no-fuzzy-matching --add-location=file "$po" po/optima-air.pot
done
echo "po/optima-air.pot updated, translations merged:" po/*.po
