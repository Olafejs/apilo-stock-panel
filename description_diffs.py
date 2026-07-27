import re
from difflib import SequenceMatcher


MAX_DIFF_CHARS = 20_000
MAX_DIFF_SEGMENTS = 800
TOKEN_RE = re.compile(r"\s+|\w+(?:[-’']\w+)*|[^\w\s]+", flags=re.UNICODE)
WORD_RE = re.compile(r"^\w+(?:[-’']\w+)*$", flags=re.UNICODE)


def _tokens(value):
    return TOKEN_RE.findall(str(value or ""))


def _token_key(token):
    if token.isspace():
        return " "
    if WORD_RE.fullmatch(token):
        return token.casefold()
    return token


def _word_count(tokens):
    return sum(1 for token in tokens if WORD_RE.fullmatch(token))


def _append_segment(segments, kind, tokens):
    text = "".join(tokens)
    if not text:
        return
    if segments and segments[-1]["kind"] == kind:
        segments[-1]["text"] += text
    else:
        segments.append({"kind": kind, "text": text})


def _empty_diff():
    return {
        "available": False,
        "changed": False,
        "similarity_percent": None,
        "missing_words": 0,
        "added_words": 0,
        "truncated": False,
        "segments": [],
    }


def build_description_diff(reference_text, channel_text):
    reference = str(reference_text or "").strip()
    channel = str(channel_text or "").strip()
    if not reference or not channel:
        return _empty_diff()

    truncated = len(reference) > MAX_DIFF_CHARS or len(channel) > MAX_DIFF_CHARS
    reference = reference[:MAX_DIFF_CHARS]
    channel = channel[:MAX_DIFF_CHARS]
    reference_tokens = _tokens(reference)
    channel_tokens = _tokens(channel)
    use_autojunk = len(reference_tokens) + len(channel_tokens) > 2_000
    matcher = SequenceMatcher(
        None,
        [_token_key(token) for token in reference_tokens],
        [_token_key(token) for token in channel_tokens],
        autojunk=use_autojunk,
    )

    segments = []
    missing_words = 0
    added_words = 0
    changed = False
    for tag, first_start, first_end, second_start, second_end in matcher.get_opcodes():
        reference_part = reference_tokens[first_start:first_end]
        channel_part = channel_tokens[second_start:second_end]
        if tag == "equal":
            _append_segment(segments, "equal", reference_part)
            continue
        changed = True
        if tag in {"delete", "replace"}:
            missing_words += _word_count(reference_part)
            _append_segment(segments, "missing", reference_part)
        if tag in {"insert", "replace"}:
            added_words += _word_count(channel_part)
            _append_segment(segments, "added", channel_part)

    reference_words = [
        _token_key(token) for token in reference_tokens if WORD_RE.fullmatch(token)
    ]
    channel_words = [
        _token_key(token) for token in channel_tokens if WORD_RE.fullmatch(token)
    ]
    similarity = SequenceMatcher(
        None,
        reference_words,
        channel_words,
        autojunk=len(reference_words) + len(channel_words) > 2_000,
    ).ratio()

    if len(segments) > MAX_DIFF_SEGMENTS:
        side = (MAX_DIFF_SEGMENTS - 1) // 2
        segments = [
            *segments[:side],
            {"kind": "equal", "text": "\n… pominięto środkową część porównania …\n"},
            *segments[-side:],
        ]
        truncated = True

    return {
        "available": True,
        "changed": changed,
        "similarity_percent": round(similarity * 100),
        "missing_words": missing_words,
        "added_words": added_words,
        "truncated": truncated,
        "segments": segments,
    }
