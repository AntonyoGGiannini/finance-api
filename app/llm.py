import json
from google import genai
from google.genai import types

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

CATEGORIAS_VALIDAS = [...]  # carrega de dim_categoria

def categorizar_batch(nomes: list[str]) -> list[dict]:
    """Recebe lista de nomes, retorna lista [{nome, categoria, subcategoria}]."""
    if not nomes:
        return []

    prompt = f"""Classifique cada lançamento abaixo em categoria e subcategoria.
Retorne APENAS um JSON array, sem markdown, no formato:
[{{"nome": "...", "categoria": "...", "subcategoria": "..."}}]

Categorias válidas: {", ".join(CATEGORIAS_VALIDAS)}

Lançamentos:
{json.dumps(nomes, ensure_ascii=False)}"""

    resp = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0,
        ),
    )
    return json.loads(resp.text)
