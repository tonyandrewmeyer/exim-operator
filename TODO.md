# Improvements to make

 * [x] Properly set up storage for the queue so it isn't lost if the unit/pod vanishes
 * [x] Relation to a database to load some values
 * [ ] More interesting configuration options, e.g. list of dnsbl
 * [ ] Other actions, e.g. manipulating the hints dbs
 * [ ] Observability stack - *partially complete* - need to figure out how to submit metrics to grafana without using a metrics endpoint
 * [x] Add storage for local mail delivery
 * [ ] Relation so that something else (e.g. mutt or roundcube or dovecot) could get the local mail. *this probably doesn't need a relation, because it's just sharing the storage. A simple Dovecot server might be an easy and nice add?*
 * [x] Convert the unit tests to use pytest
 * [ ] Something to practice with secrets (unclear what, until the data-operator library will pass the database password as a secret)
 * [ ] Add in the image generator to git
