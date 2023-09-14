CREATE TABLE IF NOT EXISTS `dkim` (
    `id` unsigned bigint PRIMARY KEY,
    `domain` varchar(255);
    `key` text;
)
