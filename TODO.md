# Improvements to make

 * [x] Properly set up storage for the queue so it isn't lost if the unit/pod vanishes
 * [ ] Relation to a database to load some values
 * [ ] More interesting configuration options, e.g. list of dnsbl
 * [ ] Other actions, e.g. manipulating the hints dbs
 * [ ] Relation to something for ingress so that a hostname can be used to connect - *This does not seem feasible*
 * [ ] Observability stack - *partially complete*
 * [x] Add storage for local mail delivery
 * [ ] Relation so that something else (e.g. mutt or roundcube or dovecot) could get the local mail. *this probably doesn't need a relation, because it's just sharing the storage. A simple Dovecot server might be an easy and nice add?*
 * [x] Convert the unit tests to use pytest
 * [ ] Something to practice with secrets, maybe a DKIM private key? - *DKIM private key is generated but not really the use-case for secrets*
