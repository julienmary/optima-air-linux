#!/bin/sh
# Installe optima-air pour l'utilisateur courant, sans paquet : un lien dans ~/bin vers le
# lanceur du dépôt, une entrée dans le menu du bureau et son icône. Le code reste dans le
# dépôt : une mise à jour du dépôt vaut mise à jour de l'application.
# Pour désinstaller : tools/install-local.sh --remove
set -eu

depot=$(dirname "$(dirname "$(readlink -f "$0")")")
identifiant=io.github.julienmary.OptimaAirLinux
lanceur="$HOME/bin/optima-air"
donnees="${XDG_DATA_HOME:-$HOME/.local/share}"
entree="$donnees/applications/$identifiant.desktop"
icone="$donnees/icons/hicolor/scalable/apps/$identifiant.svg"

if [ "${1:-}" = "--remove" ]; then
    [ -L "$lanceur" ] && rm "$lanceur"
    rm -f "$entree" "$icone"
    echo "optima-air removed from the desktop (backups and preferences kept)."
else
    if [ -e "$lanceur" ] && [ ! -L "$lanceur" ]; then
        echo "$lanceur exists and is not a link: left untouched." >&2
        exit 1
    fi
    sh "$depot/tools/build-translations.sh"
    mkdir -p "$HOME/bin" "$(dirname "$entree")" "$(dirname "$icone")"
    ln -sfn "$depot/bin/optima-air" "$lanceur"
    sed "s|@LANCEUR@|$lanceur|" "$depot/data/$identifiant.desktop" > "$entree"
    cp "$depot/data/$identifiant.svg" "$icone"
    echo "Installed: $lanceur, $entree, $icone"
fi
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$donnees/applications" 2>/dev/null || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t "$donnees/icons/hicolor" 2>/dev/null || true
fi
