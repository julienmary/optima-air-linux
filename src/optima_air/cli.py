"""Ligne de commande d'optima-air.

Les slots y sont numérotés de 1 à 23, comme sur la pédale et dans la fenêtre : la
conversion vers l'index interne (0 à 22) se fait ici et nulle part ailleurs (_numero_slot
à l'entrée, slot + 1 à l'affichage).
"""

import argparse
import locale
import os
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from optima_air import echelles, journal
from optima_air import messages_emission as emission
from optima_air.device import Connexion, Session
from optima_air.erreurs import (
    ArchiveAbimee,
    EcritureInterrompue,
    ErreurBornes,
    ErreurDisque,
    ErreurOptimaAir,
    PedaleAbsente,
)
from optima_air.i18n import _, decimal, decimal_signe, ngettext
from optima_air.messages import NB_SLOTS
from optima_air.midi import ControlChange, Evenement, ProgramChange, SysEx
from optima_air.store import FONCTIONS_A_L_ESSAI, FicheSlot, Magasin, racine_par_defaut
from optima_air.transport import PortRawmidi, Verrou, trouver_port

CARTES_ALSA = Path("/proc/asound/cards")
PRESETS_PAR_DEFAUT = (
    "Travis",
    "Paradise",
    "Circles",
    "Sunset",
    "Warn",
    "Stageman",
    "Classic",
    "Vintage",
    "3 Shades",
)
ROUTINGS = ("Reamp", "Normal", "Dry Out")
CC_POTARDS = frozenset({7, 13, 14, 15, 23, 47, 79, 87})
CC_NIVEAUX = frozenset({96, 97})


def _noms_cc() -> dict[int, str]:
    """Noms des Control Change connus, traduits à l'affichage."""
    return {
        7: "IN Vol.",
        11: "Routing",
        13: "Bass",
        14: "Middle",
        15: "Treble",
        23: "IR Level",
        47: "OUT Vol.",
        71: _("Active slot"),
        79: "Gain",
        87: "Reverb",
        101: _("IR on/off"),
        102: _("Preamp on/off"),
    }


def _numero_slot(texte: str) -> int:
    """Argument de slot : un numéro de 1 à 23, rendu en index interne de 0 à 22."""
    try:
        numero = int(texte)
    except ValueError:
        raise argparse.ArgumentTypeError(
            _("“{text}” is not a slot number").format(text=texte)
        ) from None
    if not 1 <= numero <= NB_SLOTS:
        raise argparse.ArgumentTypeError(
            _("slot {slot} out of 1..{count}").format(slot=numero, count=NB_SLOTS)
        )
    return numero - 1


def _refuser_pendant_le_silence(magasin: Magasin) -> None:
    """Après une écriture interrompue, le port n'est même pas ouvert tant que le délai de
    silence n'est pas écoulé."""
    en_cours = journal.lire(magasin.racine)
    if en_cours is None:
        return
    reste = (en_cours.fin_du_silence() - datetime.now().astimezone()).total_seconds()
    if reste > 0:
        raise EcritureInterrompue(
            _(
                "A write to slot {slot} was interrupted: the pedal is left alone for another "
                "{seconds} s. Then run: optima-air resume"
            ).format(slot=en_cours.slot + 1, seconds=decimal(reste, 0))
        )


@contextmanager
def ouvrir_session(magasin: Magasin, firmware_inconnu: bool) -> Iterator[Session]:
    """Prend le verrou, ouvre le port de la pédale et rend une session."""
    _refuser_pendant_le_silence(magasin)
    try:
        cartes = CARTES_ALSA.read_text()
    except OSError as erreur:
        raise PedaleAbsente(
            _("Cannot read {file}: {error}").format(file=CARTES_ALSA, error=erreur)
        ) from erreur
    port = PortRawmidi(trouver_port(cartes), Verrou())
    try:
        session = Session(port, magasin)
        session.firmware_debloque = firmware_inconnu
        yield session
    finally:
        port.fermer()


def _nom_preset(preset: int) -> str:
    if preset == 0x7F:
        return "MANUAL"
    if 0 <= preset < len(PRESETS_PAR_DEFAUT):
        return f"{preset + 1}. {PRESETS_PAR_DEFAUT[preset]}"
    return _("unknown ({preset})").format(preset=preset)


def _nom_routing(routing: int) -> str:
    if 0 <= routing < len(ROUTINGS):
        return ROUTINGS[routing]
    return _("unknown ({routing})").format(routing=routing)


def _afficher_etat(connexion: Connexion) -> None:
    etat = connexion.etat
    print(_("Firmware {version}").format(version=echelles.version_texte(connexion.firmware)))
    print(_("Preset: {preset}").format(preset=_nom_preset(etat.preset)))
    if 0 <= etat.slot_actif < len(connexion.slots):
        slot = _("slot {slot} “{name}”").format(
            slot=etat.slot_actif + 1, name=connexion.slots[etat.slot_actif].nom
        )
    else:
        slot = _("unknown slot ({slot})").format(slot=etat.slot_actif + 1)
    if etat.ir_active:
        print(_("IR: on, {slot}").format(slot=slot))
    else:
        print(_("IR: off, {slot}").format(slot=slot))
    print(_("Preamp: on") if etat.preamp_actif else _("Preamp: off"))
    print(
        f"Gain {etat.gain} | Bass {etat.bass} | Middle {etat.middle} | Treble {etat.treble} | "
        f"IR Level {etat.ir_level} | Reverb {etat.reverb}"
    )
    print(
        f"Routing: {_nom_routing(etat.routing)} | IN Vol. {etat.usb_in} | OUT Vol. {etat.usb_out}"
    )


def _commande_etat(arguments: argparse.Namespace, magasin: Magasin) -> int:
    with ouvrir_session(magasin, arguments.firmware_inconnu) as session:
        _afficher_etat(session.connecter())
    return 0


def _commande_slots(arguments: argparse.Namespace, magasin: Magasin) -> int:
    with ouvrir_session(magasin, arguments.firmware_inconnu) as session:
        connexion = session.connecter()
    print(
        f"  {'#':>2}  {_('Name'):<32}  {_('Ident.'):<8}  {_('Volume'):>8}  "
        f"{_('Low cut'):>7}  {_('High cut'):>8}"
    )
    for infos in connexion.slots:
        marque = "*" if infos.slot == connexion.etat.slot_actif else " "
        volume = _("{value} dB").format(
            value=decimal_signe(echelles.volume_db(connexion.volumes[infos.slot]), 1)
        )
        low_cut = _("{value} Hz").format(value=echelles.low_cut_hz(connexion.low_cuts[infos.slot]))
        high_cut = _("{value} Hz").format(
            value=echelles.high_cut_hz(connexion.high_cuts[infos.slot])
        )
        print(
            f"{marque} {infos.slot + 1:>2}  {infos.nom:<32}  {infos.identifiant:08x}  "
            f"{volume:>8}  {low_cut:>7}  {high_cut:>8}"
        )
    return 0


def _commande_lire(arguments: argparse.Namespace, magasin: Magasin) -> int:
    slot: int = arguments.slot
    with ouvrir_session(magasin, arguments.firmware_inconnu) as session:
        connexion = session.connecter()
        lecture = session.lire_slot(slot)
    infos = lecture.infos
    if not lecture.concorde:
        print(
            _(
                "Warning: the identifier announced by the pedal ({announced}) does not match "
                "the data read ({computed}), even after reading again. The backup is made "
                "anyway."
            ).format(
                announced=f"{infos.identifiant:08x}", computed=f"{lecture.identifiant_calcule:08x}"
            ),
            file=sys.stderr,
        )
    quand = datetime.now().astimezone()
    archive = magasin.creer_archive("manuelle", quand)
    magasin.ecrire_slot(
        archive,
        slot=slot,
        nom_brut=infos.nom_brut,
        identifiant=infos.identifiant,
        volume=connexion.volumes[slot],
        low_cut=connexion.low_cuts[slot],
        high_cut=connexion.high_cuts[slot],
        firmware=connexion.firmware,
        motif="manuelle",
        quand=quand,
        donnees=lecture.donnees,
    )
    print(
        _("Slot {slot} “{name}” backed up in {backup}").format(
            slot=slot + 1, name=infos.nom, backup=archive
        )
    )
    return 0


def formater_evenement(evenement: Evenement) -> str:
    if isinstance(evenement, SysEx):
        octets = evenement.octets
        if len(octets) > 40:
            texte = f"{octets[:24].hex(' ')} … {octets[-4:].hex(' ')}"
        else:
            texte = octets.hex(" ")
        return _("SysEx {size} bytes: {data}").format(size=len(octets), data=texte)
    if isinstance(evenement, ProgramChange):
        return _("Program Change channel {channel}: {program}").format(
            channel=evenement.canal + 1, program=evenement.programme
        )
    nom = _noms_cc().get(evenement.numero)
    libelle = f"CC {evenement.numero} ({nom})" if nom else f"CC {evenement.numero}"
    if evenement.canal != 0:
        libelle += " " + _("[channel {channel}]").format(channel=evenement.canal + 1)
    if evenement.numero in CC_POTARDS:
        converti = echelles.cc_vers_valeur(evenement.valeur)
        return _("{label} = {value}, that is {converted} out of 100").format(
            label=libelle, value=evenement.valeur, converted=converti
        )
    return f"{libelle} = {evenement.valeur}"


class _Niveaux:
    """Regroupe les CC de niveaux, qui arrivent toutes les 30 ms, en une ligne par seconde."""

    def __init__(self) -> None:
        self._vus: dict[int, list[int]] = {}
        self._debut: float | None = None

    def ajouter(self, instant: float, evenement: ControlChange) -> str | None:
        if self._debut is None:
            self._debut = instant
        self._vus.setdefault(evenement.numero, []).append(evenement.valeur)
        return self.vider() if instant - self._debut >= 1.0 else None

    def vider(self) -> str | None:
        if not self._vus:
            return None
        morceaux = [
            _("CC {number}: min {min} max {max} ({count} msg)").format(
                number=numero, min=min(valeurs), max=max(valeurs), count=len(valeurs)
            )
            for numero, valeurs in sorted(self._vus.items())
        ]
        self._vus = {}
        self._debut = None
        return _("levels") + "  " + " | ".join(morceaux)


def _afficher_diagnostic(session: Session) -> None:
    """Signale les pertes constatées par le port. Sans elles, ces cas resteraient invisibles."""
    port = session.transport
    abandons = getattr(port, "sysex_abandonnes", 0)
    debordements = getattr(port, "debordements", 0)
    if abandons:
        print(
            ngettext(
                "Warning: {count} SysEx dropped while being received.",
                "Warning: {count} SysEx dropped while being received.",
                abandons,
            ).format(count=abandons),
            file=sys.stderr,
        )
    if debordements:
        print(
            ngettext(
                "Warning: {count} input buffer overflow, bytes were lost.",
                "Warning: {count} input buffer overflows, bytes were lost.",
                debordements,
            ).format(count=debordements),
            file=sys.stderr,
        )


def _confirmer(question: str) -> bool:
    # La réponse « oui » suit la langue (o/oui en français), l'anglais est toujours compris.
    oui = {"y", "yes", _("y"), _("yes")}
    try:
        reponse = input(_("{question} [y/N] ").format(question=question))
    except EOFError:  # entrée fermée (tube, cron) : pas de réponse vaut non
        return False
    return reponse.strip().lower() in oui


def _abandon_sans_ecriture() -> int:
    print(_("Cancelled: nothing was written."), file=sys.stderr)
    return 1


def _commande_ecouter(arguments: argparse.Namespace, magasin: Magasin) -> int:
    if arguments.annonce and arguments.essai and not arguments.oui:
        question = _("Send the connection announcement, trial function “{name}”?").format(
            name="annonce"
        )
        if not _confirmer(question):
            print(_("Cancelled: nothing was sent."), file=sys.stderr)
            return 1
    with ouvrir_session(magasin, arguments.firmware_inconnu) as session:
        if arguments.connexion:
            _afficher_etat(session.connecter())
        elif arguments.annonce:
            session.lire_version()
        if arguments.annonce and session.annonce_emise:
            print(_("Announcement already sent by the connection sequence: not sent twice."))
        elif arguments.annonce:
            if session.annoncer(essai=arguments.essai):
                print(_("Announcement sent: answer received"))
            else:
                print(_("Announcement sent: no answer"))
        debut = time.monotonic()
        niveaux = _Niveaux()
        for evenement in session.ecouter(arguments.duree):
            instant = time.monotonic() - debut
            if isinstance(evenement, ControlChange) and evenement.numero in CC_NIVEAUX:
                ligne = niveaux.ajouter(instant, evenement)
            else:
                ligne = formater_evenement(evenement)
            if ligne is not None:
                print(f"[{instant:7.2f}] {ligne}", flush=True)
        reste = niveaux.vider()
        if reste is not None:
            print(f"[{time.monotonic() - debut:7.2f}] {reste}")
        _afficher_diagnostic(session)
    return 0


def _commande_verification(arguments: argparse.Namespace, magasin: Magasin) -> int:
    if arguments.action == "list":
        validees = magasin.fonctions_validees()
        for nom in FONCTIONS_A_L_ESSAI:
            if nom in validees:
                print(_("{name}: validated").format(name=nom))
            else:
                print(_("{name}: on trial").format(name=nom))
        return 0
    if arguments.nom is None:
        raise ErreurBornes(
            _("“trials {action}” expects the name of a function.").format(action=arguments.action)
        )
    if arguments.action == "validate":
        magasin.valider(arguments.nom)
    else:
        magasin.invalider(arguments.nom)
    return 0


def _commande_charger(arguments: argparse.Namespace, magasin: Magasin) -> int:
    from optima_air import ir  # import tardif : numpy et scipy ne servent qu'ici

    slot: int = arguments.slot
    chemin: Path = arguments.fichier
    preparee = ir.preparer_wav(chemin)
    nom: str = arguments.nom if arguments.nom is not None else chemin.stem
    try:
        emission.valider_nom_slot(nom)
    except ErreurBornes as erreur:
        raise ErreurBornes(
            _("Name “{name}” refused ({error}). Give one with --name.").format(
                name=nom, error=erreur
            )
        ) from erreur
    for avertissement in preparee.avertissements:
        print(_("Warning: {warning}").format(warning=avertissement), file=sys.stderr)
    with ouvrir_session(magasin, arguments.firmware_inconnu) as session:
        actuel = session.connecter().slots[slot]
        question = _("Overwrite slot {slot} “{current}” with “{name}” ({file})?").format(
            slot=slot + 1, current=actuel.nom, name=nom, file=chemin.name
        )
        if not arguments.oui and not _confirmer(question):
            return _abandon_sans_ecriture()
        resultat = session.ecrire_slot(slot, preparee.donnees, nom)
    print(
        _("Slot {slot}: “{name}” written and read back identical.").format(slot=slot + 1, name=nom)
    )
    print(
        _("Previous content (“{name}”) backed up in {backup}").format(
            name=actuel.nom, backup=resultat.archive
        )
    )
    return 0


def _commande_exporter(arguments: argparse.Namespace, magasin: Magasin) -> int:
    from optima_air import ir

    slot: int = arguments.slot
    cible: Path = arguments.fichier
    if cible.exists() and not arguments.oui:
        raise ErreurBornes(
            _("{file} already exists. Add --yes to overwrite it.").format(file=cible)
        )
    with ouvrir_session(magasin, arguments.firmware_inconnu) as session:
        lecture = session.lire_slot(slot)
    try:
        cible.write_bytes(ir.contenu_wav(lecture.donnees))
    except OSError as erreur:
        raise ErreurDisque(
            _("Cannot write {file}: {error}").format(file=cible, error=erreur)
        ) from erreur
    print(
        _("Slot {slot} “{name}” exported to {file}").format(
            slot=slot + 1, name=lecture.infos.nom, file=cible
        )
    )
    return 0


def _commande_sauvegarder(arguments: argparse.Namespace, magasin: Magasin) -> int:
    def progres(slot: int) -> None:
        texte = _("Reading: {done} of {total}…").format(done=slot + 1, total=NB_SLOTS)
        print(f"\r{texte}", end="", file=sys.stderr, flush=True)

    with ouvrir_session(magasin, arguments.firmware_inconnu) as session:
        try:
            sauvegarde = session.sauvegarder_tout(sur_progres=progres)
        finally:
            print(file=sys.stderr)
    for slot in sauvegarde.discordants:
        print(
            _(
                "Warning: the identifier announced for slot {slot} does not match the data "
                "read, even after reading again. It is backed up anyway."
            ).format(slot=slot + 1),
            file=sys.stderr,
        )
    print(_("All 23 slots are backed up in {backup}").format(backup=sauvegarde.archive))
    return 0


def _nom_archive(fiche: FicheSlot) -> str:
    return bytes.fromhex(fiche.nom_hex).rstrip(b"\x00").decode("latin-1")


def _commande_sauvegardes(arguments: argparse.Namespace, magasin: Magasin) -> int:
    trouvees = magasin.lister_archives()
    if not trouvees:
        print(_("No backups."))
    for archive in trouvees:
        try:
            slots = magasin.slots_presents(archive)
            if len(slots) == 1:
                contenu = _("slot {slot} “{name}”").format(
                    slot=slots[0] + 1,
                    name=_nom_archive(magasin.lire_slot(archive, slots[0])[0]),
                )
            else:
                contenu = ngettext("{count} slot", "{count} slots", len(slots)).format(
                    count=len(slots)
                )
        except ArchiveAbimee as erreur:
            contenu = _("DAMAGED ({error})").format(error=erreur)
        print(f"{archive.name}  {contenu}")
    return 0


def _liste_de_slots(slots: Sequence[int]) -> str:
    return ", ".join(str(slot + 1) for slot in slots) or _("none")


def _restaurer_toute_l_archive(arguments: argparse.Namespace, magasin: Magasin) -> int:
    archive: Path = arguments.archive
    if arguments.nom is not None:
        raise ErreurBornes(_("--name only applies to restoring a single slot."))
    slots = magasin.slots_presents(archive)
    question = ngettext(
        "Put the pedal back to the state of the backup {backup} ({count} slot)? Only the "
        "slots that differ will be rewritten, after their content is backed up.",
        "Put the pedal back to the state of the backup {backup} ({count} slots)? Only the "
        "slots that differ will be rewritten, after their content is backed up.",
        len(slots),
    ).format(backup=archive.name, count=len(slots))
    if not arguments.oui and not _confirmer(question):
        return _abandon_sans_ecriture()

    def progres(etape: str, fait: int, total: int) -> None:
        texte = _("{operation}: {done} of {total}…").format(
            operation=etape, done=fait + 1, total=total
        )
        print(f"\r{texte}  ", end="", file=sys.stderr, flush=True)

    with ouvrir_session(magasin, arguments.firmware_inconnu) as session:
        session.lire_version()
        try:
            bilan = session.restaurer_archive(archive, sur_progres=progres)
        finally:
            print(file=sys.stderr)
    print(
        _("Slots rewritten and read back identical: {slots}").format(
            slots=_liste_de_slots(bilan.reecrits)
        )
    )
    print(
        _("Slots whose settings only were restored: {slots}").format(
            slots=_liste_de_slots(bilan.regles)
        )
    )
    print(
        _("Slots already identical to the backup: {slots}").format(
            slots=_liste_de_slots(bilan.inchanges)
        )
    )
    return 0


def _commande_restaurer(arguments: argparse.Namespace, magasin: Magasin) -> int:
    if arguments.slot is None:
        return _restaurer_toute_l_archive(arguments, magasin)
    slot: int = arguments.slot
    archive: Path = arguments.archive
    fiche, _donnees = magasin.lire_slot(archive, slot)  # refuse tout de suite une archive abîmée
    archive_nom = bytes.fromhex(fiche.nom_hex).rstrip(b"\x00").decode("latin-1")
    with ouvrir_session(magasin, arguments.firmware_inconnu) as session:
        actuel = session.connecter().slots[slot]
        question = _("Rewrite slot {slot} “{current}” from the backup {backup} (“{name}”)?").format(
            slot=slot + 1,
            current=actuel.nom,
            backup=archive.name,
            name=arguments.nom or archive_nom,
        )
        if not arguments.oui and not _confirmer(question):
            return _abandon_sans_ecriture()
        resultat = session.restaurer_slot(archive, slot, nom=arguments.nom)
    print(
        _("Slot {slot} restored from {backup}, then read back identical.").format(
            slot=slot + 1, backup=archive
        )
    )
    print(
        _("Replaced content (“{name}”) backed up in {backup}").format(
            name=actuel.nom, backup=resultat.archive
        )
    )
    return 0


def _commande_reprise(arguments: argparse.Namespace, magasin: Magasin) -> int:
    with ouvrir_session(magasin, arguments.firmware_inconnu) as session:
        session.lire_version()
        constat = session.constater_reprise()
    if constat is None:
        print(_("No interrupted write."))
        return 0
    numero = constat.slot + 1
    if constat.issue == "aboutie":
        print(
            _("Slot {slot}: the interrupted write had completed. Nothing to do.").format(
                slot=numero
            )
        )
    elif constat.issue == "inchange":
        print(
            _("Slot {slot}: it still holds what it held before. Nothing to do.").format(slot=numero)
        )
    else:
        print(
            _(
                "Slot {slot}: its content is neither the old nor the new one. To put it back "
                "as it was: optima-air restore {backup} {slot}"
            ).format(slot=numero, backup=constat.archive)
        )
        return 1
    return 0


def _commande_fenetre(arguments: argparse.Namespace, magasin: Magasin) -> int:
    # Import tardif : GTK n'est chargé que pour cette commande.
    from optima_air.gui.app import lancer

    return lancer(magasin, arguments.firmware_inconnu)


_COMMANDES: dict[str, Callable[[argparse.Namespace, Magasin], int]] = {
    "window": _commande_fenetre,
    "load": _commande_charger,
    "export": _commande_exporter,
    "restore": _commande_restaurer,
    "backup": _commande_sauvegarder,
    "backups": _commande_sauvegardes,
    "resume": _commande_reprise,
    "state": _commande_etat,
    "slots": _commande_slots,
    "read": _commande_lire,
    "listen": _commande_ecouter,
    "trials": _commande_verification,
}


def _analyseur() -> argparse.ArgumentParser:
    commun = argparse.ArgumentParser(add_help=False)
    commun.add_argument(
        "--unknown-firmware",
        dest="firmware_inconnu",
        action="store_true",
        help=_("allow more than reads on a firmware that was not verified"),
    )
    commun.add_argument(
        "--trial", dest="essai", action="store_true", help=_("allow a trial function")
    )
    commun.add_argument(
        "--yes", dest="oui", action="store_true", help=_("do not ask for confirmation")
    )
    numero = _("slot number, from 1 to {count}").format(count=NB_SLOTS)

    analyseur = argparse.ArgumentParser(
        prog="optima-air", description=_("Linux editor for the NUX Optima Air pedal.")
    )
    analyseur.add_argument(
        "--data-dir",
        dest="donnees",
        metavar="DIR",
        type=Path,
        help=_("data folder (default: XDG data folder)"),
    )
    sous = analyseur.add_subparsers(dest="commande", required=True)
    sous.add_parser("window", parents=[commun], help=_("open the window"))
    sous.add_parser("state", parents=[commun], help=_("firmware version, preset and settings"))
    sous.add_parser("slots", parents=[commun], help=_("the 23 IR slots"))
    lire = sous.add_parser("read", parents=[commun], help=_("back up the IR of a slot"))
    lire.add_argument("slot", type=_numero_slot, help=numero)
    ecouter = sous.add_parser("listen", parents=[commun], help=_("print what the pedal sends"))
    ecouter.add_argument(
        "--duration",
        dest="duree",
        metavar="SECONDS",
        type=float,
        default=10.0,
        help=_("seconds of listening"),
    )
    ecouter.add_argument(
        "--connect", dest="connexion", action="store_true", help=_("read the state first")
    )
    ecouter.add_argument(
        "--announce",
        dest="annonce",
        action="store_true",
        help=_("send the announcement first (trial)"),
    )
    charger = sous.add_parser("load", parents=[commun], help=_("write a WAV into a slot"))
    charger.add_argument("slot", type=_numero_slot, help=numero)
    charger.add_argument("fichier", metavar="file", type=Path, help=_("WAV file"))
    charger.add_argument(
        "--name",
        dest="nom",
        metavar="NAME",
        help=_("name of the slot (default: the name of the file)"),
    )
    exporter = sous.add_parser("export", parents=[commun], help=_("export a slot as WAV"))
    exporter.add_argument("slot", type=_numero_slot, help=numero)
    exporter.add_argument("fichier", metavar="file", type=Path, help=_("WAV file to write"))
    restaurer = sous.add_parser(
        "restore", parents=[commun], help=_("rewrite a slot, or all of them, from a backup")
    )
    restaurer.add_argument("archive", metavar="backup", type=Path, help=_("backup folder"))
    restaurer.add_argument(
        "slot",
        type=_numero_slot,
        nargs="?",
        help=_("slot number, from 1 to {count} (default: all)").format(count=NB_SLOTS),
    )
    restaurer.add_argument(
        "--name", dest="nom", metavar="NAME", help=_("replace the backed up name")
    )
    sous.add_parser("backup", parents=[commun], help=_("back up the 23 slots"))
    sous.add_parser("backups", parents=[commun], help=_("list the backups"))
    sous.add_parser("resume", parents=[commun], help=_("check an interrupted write"))
    verification = sous.add_parser(
        "trials", parents=[commun], help=_("register of the trial functions")
    )
    verification.add_argument("action", choices=["list", "validate", "invalidate"])
    verification.add_argument("nom", metavar="name", nargs="?")
    return analyseur


def _signaler_une_ecriture_inachevee(magasin: Magasin) -> None:
    try:
        en_cours = journal.lire(magasin.racine)
    except ErreurOptimaAir:
        return
    if en_cours is not None:
        print(
            _(
                "A write to slot {slot} was left unfinished. The pedal is left alone for a "
                "while; then run: optima-air resume"
            ).format(slot=en_cours.slot + 1),
            file=sys.stderr,
        )


def main(argv: Sequence[str] | None = None) -> int:
    # Nombres écrits selon la locale (21,3 ou 21.3) ; une locale absente laisse « C ».
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    arguments = _analyseur().parse_args(argv)
    magasin = Magasin(arguments.donnees or racine_par_defaut())
    try:
        return _COMMANDES[arguments.commande](arguments, magasin)
    except ErreurOptimaAir as erreur:
        print(_("Error: {error}").format(error=erreur), file=sys.stderr)
        _signaler_une_ecriture_inachevee(magasin)
        return 1
    except BrokenPipeError:
        # Sortie coupée (par exemple « | head ») : on se tait, sans trace à la fermeture.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 1
    except KeyboardInterrupt:
        print(_("Interrupted."), file=sys.stderr)
        return 130
