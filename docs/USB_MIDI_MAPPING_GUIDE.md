# Guide de Reverse Engineering USB vers MIDI

Certains contrôleurs DJ (comme le Pioneer XDJ-AERO, utilisé ici comme exemple) ne sont pas reconnus comme des périphériques MIDI standards (USB Class-Compliant) lorsqu'ils sont branchés sur Windows. Au lieu de cela, ils envoient des paquets de données propriétaires via des points de terminaison USB (USB Endpoints) cachés ou verrouillés par des pilotes fermés.

Ce dépôt inclut des scripts Python (`dj_controller_usbpcap_extract.py` et `dj_controller_usbpcap_midi_bridge.py`) qui permettent de contourner cette limitation en :
1. Écoutant physiquement le trafic USB brut.
2. Isolant les messages correspondant à des appuis de boutons / mouvements de faders.
3. Traduisant ces messages en un signal MIDI standard reconnu par n'importe quel logiciel (Serato, Rekordbox, Traktor, etc.) via un port MIDI virtuel.

Ce guide explique comment adapter ce processus pour **n'importe quel autre contrôleur DJ**.

---

## 1. Prérequis

Pour analyser le trafic USB et créer votre propre pont MIDI, vous aurez besoin de :
- **USBPcap** : Un outil de capture de paquets USB pour Windows.
- **Wireshark** : Pour lire et analyser visuellement les trames USB capturées.
- **loopMIDI** (ou équivalent) : Pour créer un port MIDI virtuel sur Windows.
- Le matériel ciblé branché en USB.

## 2. Processus de Reverse Engineering

### Étape A : Capturer le trafic
1. Lancez Wireshark (qui utilise USBPcap en arrière-plan) et sélectionnez l'interface USB correspondant au port sur lequel votre contrôleur est branché.
2. Démarrez la capture.
3. Appuyez sur un bouton spécifique de votre contrôleur (ex: le bouton "Play" de la platine gauche). Relâchez-le. Tournez un bouton rotatif (ex: EQ Bass).
4. Arrêtez la capture.

### Étape B : Analyser les paquets
Dans Wireshark, filtrez le trafic pour masquer les requêtes de contrôle (`usb.transfer_type == 0x01` pour l'Interrupt, ou `usb.transfer_type == 0x02` pour le Bulk).

Vous cherchez des paquets qui contiennent une charge utile de données (Data Payload) courte, généralement 4 à 8 octets.
Par exemple, en appuyant sur "Play", vous pourriez voir un paquet avec la structure hexadécimale suivante :
- Appui : `09 90 40 7F`
- Relâchement : `09 80 40 00`

### Étape C : Comprendre la structure (L'exemple du XDJ-AERO)
Dans la plupart des appareils Pioneer, la structure de 4 octets est une encapsulation USB-MIDI standard (même si le driver ne la déclare pas ainsi à Windows) :
- `Byte 0 (CIN)` : Cable Number (0-15) + Code Index Number (ex: `09` = Note On).
- `Byte 1 (Status)` : Type de message MIDI et Canal (ex: `90` = Note On, Channel 1).
- `Byte 2 (Data 1)` : La note ou le numéro du Control Change (ex: `40` = Bouton Play).
- `Byte 3 (Data 2)` : La vélocité ou la valeur (ex: `7F` = Appuyé, `00` = Relâché).

Pour les platines (Jog Wheels), les messages sont souvent des "Control Change" (CC) relatifs. Par exemple, tourner à droite envoie `B0 22 01`, tourner à gauche envoie `B0 22 7F`.

## 3. Adapter les scripts pour votre contrôleur

Une fois que vous avez identifié les adresses réseau USB (Endpoints) et la structure des paquets de votre contrôleur, vous devez modifier les scripts fournis dans le dossier `scripts/` :

### A. Mettre à jour les filtres USB
Dans `dj_controller_usbpcap_extract.py`, modifiez la logique de filtrage pour qu'elle cible l'adresse de votre périphérique USB (son `Device Address` et son `Endpoint Address` trouvés dans Wireshark).

### B. Mettre à jour la logique de parsing
Tous les constructeurs n'utilisent pas la structure `CIN` de 4 octets. Si votre contrôleur envoie des blocs de 14 octets représentant l'état complet du contrôleur à chaque milliseconde, vous devrez modifier la fonction de parsing (`parse_pcap_record`) pour lire les bits spécifiques de ce gros paquet.

### C. Mettre à jour le mapping MIDI (Optionnel)
Le pont Python actuel recrache fidèlement les valeurs capturées vers un port WinMM. Si vous souhaitez modifier ce comportement (par exemple, transformer une note propriétaire compliquée en un simple signal `Note On`), vous pouvez modifier le `dj_controller_usbpcap_midi_bridge.py` pour réécrire les octets avant de les envoyer au port virtuel.

## 4. Utilisation finale dans le logiciel DJ

1. Démarrez votre script Python modifié pour créer le "pont" entre USBPcap et le port MIDI virtuel (ex: `loopMIDI Port`).
2. Ouvrez votre logiciel DJ (ex: Serato, Rekordbox).
3. Allez dans les paramètres MIDI du logiciel.
4. Activez votre port virtuel comme périphérique d'entrée MIDI.
5. Utilisez la fonction "MIDI Learn" du logiciel pour mapper manuellement vos boutons (Play, Cue, EQs) en appuyant physiquement dessus !

> **Note concernant Serato DJ Pro** : Serato requiert un périphérique principal "Certifié Serato" branché pour déverrouiller le logiciel. Ce pont logiciel MIDI ne suffit pas à débloquer Serato. De plus, le mapping manuel des Jog Wheels est restreint dans Serato pour les matériels non certifiés.
