"""Streaming parser for mysqldump ``page_props`` tuples."""


def sql_rows(values, insert_only=False):
    """Parse mysqldump VALUES tuples without executing SQL from a dump.

    Accept a string or an iterable of byte/text chunks. State spans chunks, so
    multiline INSERTs and quoted binary values remain intact. In insert_only
    mode, chunks must be physical SQL lines and other statements are skipped.
    Binary chunks use a one-to-one byte mapping: SQL syntax, relevant property
    names and Wikidata IDs are ASCII; unrelated BLOB values need no text decoding.
    """
    row, field = None, []
    quoted = False
    string_field = False
    escaped = False
    quote_pending = False
    active = not insert_only
    header = "INSERT INTO `page_props` VALUES"
    escapes = {"0": "\0", "n": "\n", "r": "\r", "t": "\t", "b": "\b", "Z": "\x1a"}
    chunks = (values,) if isinstance(values, (str, bytes)) else values
    for chunk in chunks:
        if isinstance(chunk, bytes):
            chunk = chunk.decode("latin-1")
        if not active:
            if not chunk.startswith(header):
                continue
            chunk = chunk[len(header):]
            active = True
        for char in chunk:
            if quote_pending:
                quote_pending = False
                if char == "'":
                    field.append(char)
                    continue
                else:
                    quoted = False
            if quoted:
                if escaped:
                    field.append(escapes.get(char, char))
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == "'":
                    quote_pending = True
                else:
                    field.append(char)
                continue
            if row is None:
                if char == "(":
                    row, field = [], []
                    string_field = False
                elif char == ";" and insert_only:
                    active = False
                    break
                continue
            if char == "'":
                quoted = True
                string_field = True
            elif char in ",)":
                value = "".join(field) if string_field else "".join(field).strip()
                row.append(None if not string_field and value == "NULL" else value)
                field, string_field = [], False
                if char == ")":
                    yield row
                    row = None
            elif not (string_field and char.isspace()):
                field.append(char)
    if quoted or escaped or row is not None or (insert_only and active):
        raise ValueError("Incomplete page_props SQL tuple")
