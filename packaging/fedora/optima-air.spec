%global app_id io.github.julienmary.OptimaAirLinux

Name:           optima-air
Version:        0.2.0
Release:        1%{?dist}
Summary:        Editor for the NUX Optima Air pedal: settings, impulse responses, backups

License:        GPL-3.0-or-later
URL:            https://github.com/julienmary/optima-air-linux
Source0:        %{url}/archive/v%{version}/%{name}-%{version}.tar.gz

BuildArch:      noarch

BuildRequires:  python3-devel
BuildRequires:  desktop-file-utils
BuildRequires:  gettext
BuildRequires:  help2man
BuildRequires:  libappstream-glib
# %%pyproject_check_import importe aussi la fenêtre (PyGObject, GTK 4).
BuildRequires:  python3dist(pygobject)
BuildRequires:  gtk4

# numpy, scipy et soundfile sont déduits de pyproject.toml. La fenêtre charge par
# PyGObject des typelibs qu'aucun générateur de dépendances ne couvre : GLib, Gio et
# GObject (glib2), Gtk et Gdk (gtk4), Pango (pango). D'où ces dépendances explicites,
# et le filtre de explicit-lib-dependency dans optima-air.rpmlintrc.
Requires:       glib2
Requires:       gtk4
Requires:       hicolor-icon-theme
Requires:       pango
Requires:       python3dist(pygobject)

%description
A native Linux editor for the NUX Optima Air (NAI-5) acoustic guitar pedal:
live state and settings, impulse responses (load a WAV into a slot, export a
slot), full backups and restore, from a GTK 4 window or the command line.

Not affiliated with NUX / Cherub Technology. Requires the pedal on firmware 2.3.


%prep
%autosetup -n optima-air-linux-%{version}


%generate_buildrequires
%pyproject_buildrequires


%build
%pyproject_wheel
# Les tests ne sont pas dans l'archive publique ; --check refuse un catalogue fautif.
for po in po/*.po; do
    msgfmt --check --output-file="${po%.po}.mo" "$po"
done


%install
%pyproject_install
%pyproject_save_files -l optima_air

for mo in po/*.mo; do
    langue=$(basename "$mo" .mo)
    install -Dpm 0644 "$mo" %{buildroot}%{_datadir}/locale/"$langue"/LC_MESSAGES/%{name}.mo
done
%find_lang %{name}

sed 's|@LANCEUR@|optima-air|' data/%{app_id}.desktop > %{app_id}.desktop
desktop-file-install --dir=%{buildroot}%{_datadir}/applications %{app_id}.desktop
install -Dpm 0644 data/%{app_id}.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/%{app_id}.svg
install -Dpm 0644 data/%{app_id}.metainfo.xml %{buildroot}%{_metainfodir}/%{app_id}.metainfo.xml

install -d %{buildroot}%{_mandir}/man1
LANGUAGE=C LC_ALL=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 help2man --no-info \
    --name='editor for the NUX Optima Air pedal' \
    --version-string=%{version} \
    --output=%{buildroot}%{_mandir}/man1/%{name}.1 \
    bin/optima-air


%check
%pyproject_check_import
LANGUAGE=C PYTHONDONTWRITEBYTECODE=1 bin/optima-air --help > /dev/null
desktop-file-validate %{buildroot}%{_datadir}/applications/%{app_id}.desktop
appstream-util validate-relax --nonet %{buildroot}%{_metainfodir}/%{app_id}.metainfo.xml


%files -f %{pyproject_files} -f %{name}.lang
%doc README.md
%{_bindir}/optima-air
%{_datadir}/applications/%{app_id}.desktop
%{_datadir}/icons/hicolor/scalable/apps/%{app_id}.svg
%{_metainfodir}/%{app_id}.metainfo.xml
%{_mandir}/man1/%{name}.1*


%changelog
* Sat Nov 28 2026 Julien MARY <julien.mary@gmail.com> - 0.2.0-1
- First RPM package: interface translated into 76 languages, settings,
  impulse responses, backups and restore.
