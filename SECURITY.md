# Sécurité

Ne placez jamais de registre musical réel, base DJ, jeton, cookie, fichier
d’environnement ou clé privée dans Git. Les scripts doivent lire les identifiants
de service depuis l’environnement ou le mécanisme sécurisé de l’utilisateur.
Les noms de variables d’authentification ne sont pas des valeurs de secrets.

Avant publication, scanner l’arbre et l’historique, examiner les fichiers ajoutés,
exécuter les tests et exclure les chemins personnels et données locales. Aucun
scanner ne garantit à lui seul l’absence absolue de secrets.

Pour signaler une vulnérabilité, privilégiez le signalement privé GitHub lorsqu’il
est disponible. Ne publiez jamais de secret ou de bibliothèque musicale dans une issue.
