"""Edition-local primary-topic links shared by the importers."""


def primary_topic_title(title, profile):
    for suffix in sorted(profile.get("disambiguation_suffixes", []), key=len, reverse=True):
        if title.endswith(suffix):
            return title[:-len(suffix)].strip() or None
    return None


def primary_topic_evidence(language, article_title, dab_title, **extra):
    return {"relation": "primary-topic-title-convention", "derived": True,
            "source_language": language, "article_title": article_title,
            "disambiguation_title": dab_title,
            "rule": "Configured disambiguation suffix; primary title resolves to an ordinary article in the same edition.",
            **extra}


def staged_primary_topics(con, profiles):
    """Resolve configured primary titles using the staging redirect map."""
    for language, profile in profiles.items():
        for suffix in profile.get("disambiguation_suffixes", []):
            yield from con.execute("""SELECT a.id,d.id,a.title,d.title,d.language
                FROM graph_nodes d JOIN resolved r
                  ON r.language=d.language AND r.title=trim(substr(d.title,1,length(d.title)-?))
                JOIN graph_nodes a ON a.id=r.node_id
                WHERE d.language=? AND d.type='disambiguation'
                  AND substr(d.title,-?)=? AND a.type='article' AND a.language=d.language""",
                (len(suffix), language, len(suffix), suffix))
