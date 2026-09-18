"""Preserve source-backed Python reproductions when rendering released fragments."""
import ast


def fragment_text(fragment):
    text = fragment['text']
    quote = fragment['source_quote']
    if quote.replace('\r\n', '\n') in text.replace('\r\n', '\n'):
        return text
    # Recognize only complete multiline Python units, never arbitrary prose or
    # single identifiers. This does not execute source or infer missing imports.
    if fragment['category'] != 'symptom' or '\n' not in quote:
        return text
    if quote.strip().startswith('```') and quote.strip().endswith('```'):
        return text + '\n\n' + quote
    try:
        tree = ast.parse(quote)
    except SyntaxError:
        return text
    if not any(isinstance(node, (ast.For, ast.With, ast.Try, ast.Assert)) for node in tree.body):
        return text
    return text + '\n\n原始复现：\n```python\n' + quote + '\n```'
