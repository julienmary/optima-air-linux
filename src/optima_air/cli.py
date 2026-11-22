"""Ligne de commande d'optima-air (tranche 1 : lecture seule)."""

import argparse
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
NOMS_CC = {
    7: "IN Vol.",
    11: "Routing",
    13: "Bass",
    14: "Middle",
    15: "Treble",
    23: "IR Level",
    47: "OUT Vol.",
    71: "Slot actif",
    79: "Gain",
    87: "Reverb",
    101: "IR marche/arrêt",
    102: "Preamp marche/arrêt",
}
CC_POTARDS = frozenset({7, 13, 14, 15, 23, 47, 79, 87})
CC_NIVEAUX = frozenset({96, 97})


def _refuser_pendant_le_silence(magasin: Magasin) -> None:
    """Après une écriture interrompue, le port n'est même pas ouvert tant que le délai de
    silence n'est pas écoulé."""
    en_cours = journal.lire(magasin.racine)
    if en_cours is None:
        return
    reste = (en_cours.fin_du_silence() - datetime.now().astimezone()).total_seconds()
    if reste > 0:
        raise EcritureInterrompue(
            f"Une écriture du slot {en_cours.slot} a été interrompue : la pédale est laissée "
            f"tranquille encore {reste:.0f} s. Ensuite : optima-air reprise"
        )


@contextmanager
def ouvrir_session(magasin: Magasin, firmware_inconnu: bool) -> Iterator[Session]:
    """Prend le verrou, ouvre le port de la pédale et rend une session."""
    _refuser_pendant_le_silence(magasin)
    try:
        cartes = CARTES_ALSA.read_text()
    except OSError as erreur:
        raise PedaleAbsente(f"Impossible de lire {CARTES_ALSA} : {erreur}") from erreur
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
    return f"inconnu ({preset})"


def _nom_routing(routing: int) -> str:
    return ROUTINGS[routing] if 0 <= routing < len(ROUTINGS) else f"inconnu ({routing})"


def _afficher_etat(connexion: Connexion) -> None:
    etat = connexion.etat
    print(f"Firmware {echelles.version_texte(connexion.firmware)}")
    print(f"Preset : {_nom_preset(etat.preset)}")
    if 0 <= etat.slot_actif < len(connexion.slots):
        slot = f"slot {etat.slot_actif} « {connexion.slots[etat.slot_actif].nom} »"
    else:
        slot = f"slot inconnu ({etat.slot_actif})"
    print(f"IR : {'active' if etat.ir_active else 'coupée'}, {slot}")
    print(f"Preamp : {'actif' if etat.preamp_actif else 'coupé'}")
    print(
        f"Gain {etat.gain} | Bass {etat.bass} | Middle {etat.middle} | Treble {etat.treble} | "
        f"IR Level {etat.ir_level} | Reverb {etat.reverb}"
    )
    print(
        f"Routing : {_nom_routing(etat.routing)} | IN Vol. {etat.usb_in} | OUT Vol. {etat.usb_out}"
    )


def _commande_etat(arguments: argparse.Namespace, magasin: Magasin) -> int:
    with ouvrir_session(magasin, arguments.firmware_inconnu) as session:
        _afficher_etat(session.connecter())
    return 0


def _commande_slots(arguments: argparse.Namespace, magasin: Magasin) -> int:
    with ouvrir_session(magasin, arguments.firmware_inconnu) as session:
        connexion = session.connecter()
    print(f"  {'#':>2}  {'Nom':<32}  {'Ident.':<8}  {'Volume':>8}  {'Low cut':>7}  {'High cut':>8}")
    for infos in connexion.slots:
        marque = "*" if infos.slot == connexion.etat.slot_actif else " "
        volume = f"{echelles.volume_db(connexion.volumes[infos.slot]):+.1f} dB"
        low_cut = f"{echelles.low_cut_hz(connexion.low_cuts[infos.slot])} Hz"
        high_cut = f"{echelles.high_cut_hz(connexion.high_cuts[infos.slot])} Hz"
        print(
            f"{marque} {infos.slot:>2}  {infos.nom:<32}  {infos.identifiant:08x}  "
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
            f"Attention : l'identifiant annoncé par la pédale ({infos.identifiant:08x}) ne "
            f"correspond pas aux données lues ({lecture.identifiant_calcule:08x}), même après "
            "relecture. L'archive est faite quand même.",
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
    print(f"Slot {slot} « {infos.nom} » archivé dans {archive}")
    return 0


def formater_evenement(evenement: Evenement) -> str:
    if isinstance(evenement, SysEx):
        octets = evenement.octets
        if len(octets) > 40:
            texte = f"{octets[:24].hex(' ')} … {octets[-4:].hex(' ')}"
        else:
            texte = octets.hex(" ")
        return f"SysEx {len(octets)} o. : {texte}"
    if isinstance(evenement, ProgramChange):
        return f"Program Change canal {evenement.canal + 1} : {evenement.programme}"
    nom = NOMS_CC.get(evenement.numero)
    libelle = f"CC {evenement.numero} ({nom})" if nom else f"CC {evenement.numero}"
    if evenement.canal != 0:
        libelle += f" [canal {evenement.canal + 1}]"
    if evenement.numero in CC_POTARDS:
        converti = echelles.cc_vers_valeur(evenement.valeur)
        return f"{libelle} = {evenement.valeur}, soit {converti} sur 100"
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
            f"CC {numero} : min {min(valeurs)} max {max(valeurs)} ({len(valeurs)} msg)"
            for numero, valeurs in sorted(self._vus.items())
        ]
        self._vus = {}
        self._debut = None
        return "niveaux  " + " | ".join(morceaux)


def _afficher_diagnostic(session: Session) -> None:
    """Signale les pertes constatées par le port. Sans elles, ces cas resteraient invisibles."""
    port = session.transport
    abandons = getattr(port, "sysex_abandonnes", 0)
    debordements = getattr(port, "debordements", 0)
    if abandons:
        print(f"Attention : {abandons} SysEx abandonné(s) en cours de réception.", file=sys.stderr)
    if debordements:
        print(
            f"Attention : {debordements} débordement(s) du tampon d'entrée, des octets ont "
            "été perdus.",
            file=sys.stderr,
        )


def _confirmer(question: str) -> bool:
    try:
        return input(f"{question} [o/N] ").strip().lower() in ("o", "oui")
    except EOFError:  # entrée fermée (tube, cron) : pas de réponse vaut non
        return False


def _commande_ecouter(arguments: argparse.Namespace, magasin: Magasin) -> int:
    if arguments.annonce and arguments.essai and not arguments.oui:
        question = "Émettre l'annonce de connexion, fonction à l'essai « annonce » ?"
        if not _confirmer(question):
            print("Abandon : rien n'a été émis.", file=sys.stderr)
            return 1
    with ouvrir_session(magasin, arguments.firmware_inconnu) as session:
        if arguments.connexion:
            _afficher_etat(session.connecter())
        elif arguments.annonce:
            session.lire_version()
        if arguments.annonce and session.annonce_emise:
            print("Annonce déjà émise par la séquence de connexion : pas de second envoi.")
        elif arguments.annonce:
            repondu = session.annoncer(essai=arguments.essai)
            print(f"Annonce émise : {'réponse reçue' if repondu else 'pas de réponse'}")
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
    if arguments.action == "liste":
        validees = magasin.fonctions_validees()
        for nom in FONCTIONS_A_L_ESSAI:
            print(f"{nom} : {'validée' if nom in validees else "à l'essai"}")
        return 0
    if arguments.nom is None:
        raise ErreurBornes(f"« verification {arguments.action} » attend le nom d'une fonction.")
    if arguments.action == "valider":
        magasin.valider(arguments.nom)
    else:
        magasin.invalider(arguments.nom)
    return 0


def _slot_valide(slot: int) -> int:
    if not 0 <= slot <= 22:
        raise ErreurBornes(f"slot {slot} hors de 0..22")
    return slot


def _commande_charger(arguments: argparse.Namespace, magasin: Magasin) -> int:
    from optima_air import ir  # import tardif : numpy et scipy ne servent qu'ici

    slot = _slot_valide(arguments.slot)
    chemin: Path = arguments.fichier
    preparee = ir.preparer_wav(chemin)
    nom: str = arguments.nom if arguments.nom is not None else chemin.stem
    try:
        emission.valider_nom_slot(nom)
    except ErreurBornes as erreur:
        raise ErreurBornes(f"Nom « {nom} » refusé ({erreur}). Donne-en un avec --nom.") from erreur
    for avertissement in preparee.avertissements:
        print(f"Attention : {avertissement}", file=sys.stderr)
    with ouvrir_session(magasin, arguments.firmware_inconnu) as session:
        actuel = session.connecter().slots[slot]
        question = f"Écraser le slot {slot} « {actuel.nom} » par « {nom} » ({chemin.name}) ?"
        if not arguments.oui and not _confirmer(question):
            print("Abandon : rien n'a été écrit.", file=sys.stderr)
            return 1
        resultat = session.ecrire_slot(slot, preparee.donnees, nom)
    print(f"Slot {slot} : « {nom} » écrit, puis relu à l'identique.")
    print(f"Ancien contenu (« {actuel.nom} ») archivé dans {resultat.archive}")
    return 0


def _commande_exporter(arguments: argparse.Namespace, magasin: Magasin) -> int:
    from optima_air import ir

    slot = _slot_valide(arguments.slot)
    cible: Path = arguments.fichier
    if cible.exists() and not arguments.oui:
        raise ErreurBornes(f"{cible} existe déjà. Ajoute --oui pour l'écraser.")
    with ouvrir_session(magasin, arguments.firmware_inconnu) as session:
        lecture = session.lire_slot(slot)
    try:
        cible.write_bytes(ir.contenu_wav(lecture.donnees))
    except OSError as erreur:
        raise ErreurDisque(f"Impossible d'écrire {cible} : {erreur}") from erreur
    print(f"Slot {slot} « {lecture.infos.nom} » exporté dans {cible}")
    return 0


def _commande_sauvegarder(arguments: argparse.Namespace, magasin: Magasin) -> int:
    def progres(slot: int) -> None:
        print(f"\rLecture : {slot + 1} sur 23…", end="", file=sys.stderr, flush=True)

    with ouvrir_session(magasin, arguments.firmware_inconnu) as session:
        try:
            sauvegarde = session.sauvegarder_tout(sur_progres=progres)
        finally:
            print(file=sys.stderr)
    for slot in sauvegarde.discordants:
        print(
            f"Attention : l'identifiant annoncé pour le slot {slot} ne correspond pas aux "
            "données lues, même après relecture. Il est archivé quand même.",
            file=sys.stderr,
        )
    print(f"Les 23 slots sont sauvegardés dans {sauvegarde.archive}")
    return 0


def _nom_archive(fiche: FicheSlot) -> str:
    return bytes.fromhex(fiche.nom_hex).rstrip(b"\x00").decode("latin-1")


def _commande_sauvegardes(arguments: argparse.Namespace, magasin: Magasin) -> int:
    trouvees = magasin.lister_archives()
    if not trouvees:
        print("Aucune sauvegarde.")
    for archive in trouvees:
        try:
            slots = magasin.slots_presents(archive)
            if len(slots) == 1:
                contenu = (
                    f"slot {slots[0]} « {_nom_archive(magasin.lire_slot(archive, slots[0])[0])} »"
                )
            else:
                contenu = f"{len(slots)} slots"
        except ArchiveAbimee as erreur:
            contenu = f"ABÎMÉE ({erreur})"
        print(f"{archive.name}  {contenu}")
    return 0


def _restaurer_toute_l_archive(arguments: argparse.Namespace, magasin: Magasin) -> int:
    archive: Path = arguments.archive
    if arguments.nom is not None:
        raise ErreurBornes("--nom ne vaut que pour la restauration d'un seul slot.")
    slots = magasin.slots_presents(archive)
    question = (
        f"Remettre la pédale dans l'état de l'archive {archive.name} ({len(slots)} slot(s)) ? "
        "Seuls les slots qui diffèrent seront réécrits, après archivage de leur contenu."
    )
    if not arguments.oui and not _confirmer(question):
        print("Abandon : rien n'a été écrit.", file=sys.stderr)
        return 1

    def progres(etape: str, fait: int, total: int) -> None:
        print(f"\r{etape} : {fait + 1} sur {total}…  ", end="", file=sys.stderr, flush=True)

    with ouvrir_session(magasin, arguments.firmware_inconnu) as session:
        session.lire_version()
        try:
            bilan = session.restaurer_archive(archive, sur_progres=progres)
        finally:
            print(file=sys.stderr)

    def liste(slots: Sequence[int]) -> str:
        return ", ".join(str(slot) for slot in slots) or "aucun"

    print(f"Slots réécrits puis relus à l'identique : {liste(bilan.reecrits)}")
    print(f"Slots dont seuls les réglages ont été remis : {liste(bilan.regles)}")
    print(f"Slots déjà identiques à l'archive : {liste(bilan.inchanges)}")
    return 0


def _commande_restaurer(arguments: argparse.Namespace, magasin: Magasin) -> int:
    if arguments.slot is None:
        return _restaurer_toute_l_archive(arguments, magasin)
    slot = _slot_valide(arguments.slot)
    archive: Path = arguments.archive
    fiche, _ = magasin.lire_slot(archive, slot)  # refuse tout de suite une archive abîmée
    archive_nom = bytes.fromhex(fiche.nom_hex).rstrip(b"\x00").decode("latin-1")
    with ouvrir_session(magasin, arguments.firmware_inconnu) as session:
        actuel = session.connecter().slots[slot]
        question = (
            f"Réécrire le slot {slot} « {actuel.nom} » depuis l'archive {archive.name} "
            f"(« {arguments.nom or archive_nom} ») ?"
        )
        if not arguments.oui and not _confirmer(question):
            print("Abandon : rien n'a été écrit.", file=sys.stderr)
            return 1
        resultat = session.restaurer_slot(archive, slot, nom=arguments.nom)
    print(f"Slot {slot} restauré depuis {archive}, puis relu à l'identique.")
    print(f"Contenu remplacé (« {actuel.nom} ») archivé dans {resultat.archive}")
    return 0


def _commande_reprise(arguments: argparse.Namespace, magasin: Magasin) -> int:
    with ouvrir_session(magasin, arguments.firmware_inconnu) as session:
        session.lire_version()
        constat = session.constater_reprise()
    if constat is None:
        print("Aucune écriture interrompue.")
    elif constat.issue == "aboutie":
        print(f"Slot {constat.slot} : l'écriture interrompue avait abouti. Rien à faire.")
    elif constat.issue == "inchange":
        print(f"Slot {constat.slot} : il contient toujours ce qu'il y avait avant. Rien à faire.")
    else:
        print(
            f"Slot {constat.slot} : son contenu n'est ni l'ancien ni le nouveau. Pour le "
            f"remettre comme avant : optima-air restaurer {constat.archive} {constat.slot}"
        )
        return 1
    return 0


def _commande_fenetre(arguments: argparse.Namespace, magasin: Magasin) -> int:
    # Import tardif : GTK n'est chargé que pour cette commande.
    from optima_air.gui.app import lancer

    return lancer(magasin, arguments.firmware_inconnu)


_COMMANDES: dict[str, Callable[[argparse.Namespace, Magasin], int]] = {
    "fenetre": _commande_fenetre,
    "charger": _commande_charger,
    "exporter": _commande_exporter,
    "restaurer": _commande_restaurer,
    "sauvegarder": _commande_sauvegarder,
    "sauvegardes": _commande_sauvegardes,
    "reprise": _commande_reprise,
    "etat": _commande_etat,
    "slots": _commande_slots,
    "lire": _commande_lire,
    "ecouter": _commande_ecouter,
    "verification": _commande_verification,
}


def _analyseur() -> argparse.ArgumentParser:
    commun = argparse.ArgumentParser(add_help=False)
    commun.add_argument(
        "--firmware-inconnu",
        action="store_true",
        help="autorise autre chose que des lectures sur un firmware non vérifié",
    )
    commun.add_argument("--essai", action="store_true", help="autorise une fonction à l'essai")
    commun.add_argument("--oui", action="store_true", help="ne demande pas de confirmation")

    analyseur = argparse.ArgumentParser(
        prog="optima-air", description="Éditeur Linux pour la pédale NUX Optima Air."
    )
    analyseur.add_argument("--donnees", type=Path, help="dossier de données (par défaut : XDG)")
    sous = analyseur.add_subparsers(dest="commande", required=True)
    sous.add_parser("fenetre", parents=[commun], help="ouvre la fenêtre graphique")
    sous.add_parser("etat", parents=[commun], help="version, preset et réglages")
    sous.add_parser("slots", parents=[commun], help="les 23 slots d'IR")
    lire = sous.add_parser("lire", parents=[commun], help="archive l'IR d'un slot")
    lire.add_argument("slot", type=int, help="numéro du slot, de 0 à 22")
    ecouter = sous.add_parser("ecouter", parents=[commun], help="affiche ce que la pédale émet")
    ecouter.add_argument("--duree", type=float, default=10.0, help="secondes d'écoute")
    ecouter.add_argument("--connexion", action="store_true", help="lit d'abord l'état")
    ecouter.add_argument("--annonce", action="store_true", help="émet d'abord l'annonce (essai)")
    charger = sous.add_parser("charger", parents=[commun], help="écrit un WAV dans un slot")
    charger.add_argument("slot", type=int, help="numéro du slot, de 0 à 22")
    charger.add_argument("fichier", type=Path, help="fichier WAV")
    charger.add_argument("--nom", help="nom du slot (par défaut : celui du fichier)")
    exporter = sous.add_parser("exporter", parents=[commun], help="exporte un slot en WAV")
    exporter.add_argument("slot", type=int, help="numéro du slot, de 0 à 22")
    exporter.add_argument("fichier", type=Path, help="fichier WAV à écrire")
    restaurer = sous.add_parser(
        "restaurer", parents=[commun], help="réécrit un slot, ou tous, depuis une archive"
    )
    restaurer.add_argument("archive", type=Path, help="dossier d'archive")
    restaurer.add_argument(
        "slot", type=int, nargs="?", help="numéro du slot, de 0 à 22 (par défaut : tous)"
    )
    restaurer.add_argument("--nom", help="remplace le nom archivé")
    sous.add_parser("sauvegarder", parents=[commun], help="sauvegarde les 23 slots")
    sous.add_parser("sauvegardes", parents=[commun], help="liste les sauvegardes")
    sous.add_parser("reprise", parents=[commun], help="constate une écriture interrompue")
    verification = sous.add_parser(
        "verification", parents=[commun], help="registre des fonctions à l'essai"
    )
    verification.add_argument("action", choices=["liste", "valider", "invalider"])
    verification.add_argument("nom", nargs="?")
    return analyseur


def _signaler_une_ecriture_inachevee(magasin: Magasin) -> None:
    try:
        en_cours = journal.lire(magasin.racine)
    except ErreurOptimaAir:
        return
    if en_cours is not None:
        print(
            f"Une écriture du slot {en_cours.slot} est restée inachevée. La pédale est "
            "laissée tranquille un moment ; ensuite, lance : optima-air reprise",
            file=sys.stderr,
        )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _analyseur().parse_args(argv)
    magasin = Magasin(arguments.donnees or racine_par_defaut())
    try:
        return _COMMANDES[arguments.commande](arguments, magasin)
    except ErreurOptimaAir as erreur:
        print(f"Erreur : {erreur}", file=sys.stderr)
        _signaler_une_ecriture_inachevee(magasin)
        return 1
    except BrokenPipeError:
        # Sortie coupée (par exemple « | head ») : on se tait, sans trace à la fermeture.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 1
    except KeyboardInterrupt:
        print("Interrompu.", file=sys.stderr)
        return 130
