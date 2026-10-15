# Interface locale minimale

Version expérimentale : trois écrans, **Bibliothèque → Préparer → Clé USB**.
La page appelle des commandes réelles configurées localement. Aucun exemple
chiffré ni résultat fictif n’est affiché dans le mode normal.

## Démarrer

Python 3.10+ suffit au serveur. Les commandes DJ conservent leurs dépendances.
Copier `config/local-ui.json.example` vers `config/local-ui.json`, puis renseigner
des commandes de confiance sous forme de tableaux d’arguments, jamais de shell.
Le modèle démarre sans commandes : il permet de visiter l’interface, avec les
opérations désactivées jusqu’à leur configuration.

```powershell
python scripts/dj_local_ui.py --config config/local-ui.json
```

Sous Windows, `scripts/open_dj_ui.pyw` lance le serveur masqué et ouvre le
navigateur. Une association Python est nécessaire pour le double-clic ; sinon
utiliser `pythonw scripts/open_dj_ui.pyw`. L’interface doit être arrêtée avec son
bouton de fermeture ; fermer l’onglet seul laisse le serveur local disponible.

Ouvrir l’adresse locale affichée. Un port libre est choisi automatiquement et
enregistré dans `runtime/local-ui/server.json`. Le serveur écoute uniquement `127.0.0.1`.
Pour arrêter, utiliser **Arrêter l’interface** après la fin des opérations, ou
Ctrl+C dans le terminal de développement. Aucun démarrage automatique.
Pour connaître son état, consulter `/api/state` à cette adresse. Si une copie
est active, demander d’abord son arrêt contrôlé ; la fermeture du serveur attend
la sortie de la commande. Fermer seulement l’onglet ne termine pas le serveur.

## Contrat des commandes

- `audit` : lecture de la bibliothèque, copies cohérentes et rapport local.
- `prepare` : construction/vérification locale éventuelle, puis aperçu scellé de
  la copie USB. Le dernier rapport fournit `status: preview`, `identity` et `counts`.
- `sync` : applique exactement cet aperçu et conserve les garde-fous de
  l’exporteur. Son rapport doit terminer avec `status: pass`.

Chaque tâche déclare `commands` (tableaux d’arguments), `cwd`, `report`. Le rapport
d’audit doit terminer avec `status: pass`. Une seule tâche s’exécute à la fois.
Un audit ou une préparation invalide l’aperçu précédent. Les fichiers `sealFiles`
et le rapport d’aperçu sont hachés après préparation et revérifiés avant copie.
La confirmation est à usage unique. `sync.stopFile` délègue l’arrêt coopératif à
l’exporteur ; l’interface ne tue pas une copie USB en cours.

Les configurations, rapports et journaux restent locaux, ignorés par Git.
Seul un utilisateur de confiance doit modifier la configuration : elle désigne
les programmes exécutables. Le navigateur ne peut fournir ni commande ni chemin.
Le serveur exige un hôte local, une origine identique et un jeton de session pour
les actions. Ne pas exposer ce serveur avec un tunnel ou sur le réseau.

## Périmètre de cette première version

L’interface lance les trois opérations, affiche leurs rapports et contrôle leur
enchaînement. Elle ne remplace pas les analyses natives, l’exporteur, les sauvegardes
ni l’arbitrage musical. Elle ne classe pas automatiquement les titres sans bac.
Le dépôt ne fournit pas d’exporteur natif complet ni de configuration universelle.
L’adaptateur exige un exporteur compatible déjà installé, son environnement
Python et des paramètres locaux de bibliothèque et de volumes. Les fichiers
de configuration réels et les données musicales ne sont pas distribués.
L’adaptateur `dj_ui_native_adapter.py` crée une copie cohérente de la collection,
extrait l’arbre USB courant et prépare un nouveau manifeste depuis cette copie.
Il compare l’export local à ces données fraîches avant le plan de copie. S’il ne
correspond plus, le lot doit être reconstruit avec l’exporteur puis revérifié.
Cette version ne pilote pas les logiciels d’analyse ni l’ajout automatique de
nouveaux fichiers dans Rekordbox. Le profil natif installé conserve ses identités
de volumes et ses prérequis, dont une éventuelle clé de référence.

Les essais sur une cible temporaire ne valent pas acceptation sur un lecteur.
L’état final est **Copie vérifiée**, jamais « Testée sur le lecteur » automatiquement.
Au redémarrage, le dernier contrôle peut rester visible, mais il ne débloque
jamais la préparation : une nouvelle vérification est requise dans la session.

## Vérification du développement

`python -m unittest discover -s tests -v` exécute les tests sans bibliothèque privée.
`tests/local_ui.browser.cjs` complète ces tests avec Playwright et une cible
temporaire : trois écrans bureau/mobile, annulation, copie confirmée et panne
réseau. Il refuse une cible qui n’est pas explicitement nommée temporaire.
Les captures et le manifeste restent dans les outputs locaux du test.

## Structure et direction visuelle

HTML/CSS/JavaScript sans framework, servis par Python standard : aucun npm ni
installateur nécessaire. Ce choix réduit les dépendances pour trois petits écrans.
Fond clair, une navigation, une action principale par écran, détails repliés,
menus et aides en français. Les listes de détails sont bornées ; les rapports
complets restent sur disque. Vérification bureau et mobile.

Source de comportement : `scripts/dj_local_ui.py`. Surface : `local_ui/index.html`,
`app.js`, `style.css`. La configuration locale choisit les commandes existantes ;
elle ne modifie pas leurs garde-fous. Les captures de validation restent des
artefacts de test. L’interface est distribuée en code source, avec la ligne de
commande comme référence ; aucun installateur ni service hébergé n’est fourni.
