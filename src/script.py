#!/usr/bin/env python3
"""Genera el guion con un LLM gratuito SOLO a partir de data/hechos.json y valida cada cifra.

Uso (desde la raíz del repositorio, después de fetch.py y facts.py):
    python src/script.py                              # genera data/guion.json y data/guion.txt
    python src/script.py --estructura cronologica     # otra estructura narrativa
    python src/script.py --proveedor cloudflare       # forzar proveedor
    python src/script.py --validar data/guion.json    # revalidar un guion (p. ej. editado a mano)
    python src/script.py --listar-modelos             # modelos de Gemini disponibles para tu clave

Claves (NUNCA en el chat ni en archivos; van como secretos de GitHub o variables de entorno):
    GEMINI_API_KEY
    CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

from fetch import DATA, cargar_config

VISUALES = [
    "linea_con_anotaciones",
    "carrera_barras",
    "grafico_pendientes",
    "mapa_coropletico",
    "pequenos_multiplos",
]

ESTRUCTURAS = {
    "pregunta_misterio_resolucion": (
        "Pregunta, misterio y resolución. Abre con la pregunta y un dato que genere intriga. "
        "Avanza con pistas tomadas de los hechos, de la menos a la más sorprendente. "
        "Resuelve con la tesis que los datos sostienen."
    ),
    "cronologica": (
        "Cronológica. Recorre el periodo del año inicial al final en etapas, usando solo años "
        "que aparezcan en los hechos. Cada etapa añade un dato que cambia la lectura de la anterior."
    ),
    "comparacion": (
        "Comparación. Contrasta dos o tres países (o los extremos del grupo) con las mismas "
        "métricas, escena por escena, hasta mostrar qué los separa."
    ),
    "mito_vs_dato": (
        "Expectativa frente a dato. Plantea una expectativa razonable como pregunta (sin afirmar "
        "qué cree 'la gente') y contrástala con lo que muestran los datos."
    ),
}

FRASES_CAUSALES = [
    "debido a", "gracias a", "a causa de", "por culpa de", "se debe a", "se debió a",
    "provocó", "provocado por", "causado por", "causó", "como consecuencia de", "por efecto de",
]
COBERTURAS = [
    "podría", "podrían", "quizá", "quizás", "tal vez", "posible", "hipótesis",
    "no sabemos", "no explican", "no permiten", "no dicen",
]
PALABRAS_PROHIBIDAS = [
    "increíble", "impactante", "brutal", "explosivo", "escandaloso",
    "no vas a creer", "te sorprenderá", "te dejará sin palabras",
]

SISTEMA = """Eres guionista de un canal de YouTube de datos abiertos en español neutro latinoamericano. \
Tono claro, sobrio y curioso. Tu texto se leerá en voz alta.

REGLAS ESTRICTAS:
1. Usa ÚNICAMENTE las cifras del bloque HECHOS. No calcules cifras nuevas, no estimes, no uses cifras ni \
años ni décadas de memoria. Copia cada cifra tal como aparece (puedes mostrarla con menos decimales solo \
si es la misma cifra redondeada).
2. Escribe todas las cifras con dígitos. Decimal con coma (74,3). Sin separador de miles. Los años sin \
separador (1960). Escribe "por ciento" en lugar del símbolo %. No cuentes elementos con números que no \
estén en los hechos; si no hace falta contar, no cuentes.
3. No afirmes causas ni contexto histórico (guerras, políticas, epidemias, crisis...). Los hechos no los \
contienen. Si mencionas una posible explicación, preséntala solo como hipótesis ("podría deberse a...") \
o di que los datos no explican el porqué. Distingue siempre correlación de causalidad.
4. No des consejos de salud, finanzas ni legales. Describe poblaciones, no recomendaciones. No dramatices.
5. Sin exclamaciones, sin muletillas ni clickbait ("increíble", "impactante"). Título honesto, en forma de \
pregunta o de tesis que los datos sostengan.
6. La primera escena (los primeros 10 segundos) plantea la pregunta y un dato sorprendente tomado de los \
hechos. El video defiende UNA tesis propia que los datos sostengan.
7. La última escena nombra la fuente (Banco Mundial), el último año de datos y las limitaciones. \
Nunca sugieras que el Banco Mundial respalda, patrocina o participa en el video.
8. Frases cortas, aptas para locución. Sin paréntesis, tablas ni siglas.
9. Los nombres de países aparecen en inglés en los hechos: en el texto escríbelos en español correcto \
(por ejemplo "Brazil" se dice "Brasil"). En el campo "foco" copia el nombre EXACTO que aparece en los hechos.
10. TRAZABILIDAD: cada cifra que escribas en el texto debe declararse en el campo "cifras" como \
{"valor": "74,3", "ruta": "paises_clave.BRA.valor_final"}. "valor" es la cifra tal como la escribiste y "ruta" es el \
camino con puntos hasta esa cifra dentro de HECHOS (las listas se indexan desde 0: \
grupo.mayores_ganancias_absolutas.0.cambio_absoluto; también meta.anio_final, grupo.dispersion_final.rango). \
Una ruta que apunte a una lista da su cantidad de elementos. Las cifras del título, la pregunta o la tesis \
van en el "cifras" de la raíz; las de cada escena, en el "cifras" de esa escena. Un programa comprueba cada \
ruta: si la ruta no existe o su valor no coincide, el guion se rechaza.
11. Responde SOLO con un objeto JSON válido, sin texto antes ni después, sin bloques de código."""


# ----------------------------------------------------------------------------
# Paquete de hechos que ve el LLM (y contra el que se validan las cifras)
# ----------------------------------------------------------------------------
def paquete_hechos(h):
    g, por_pais = h["grupo"], h["por_pais"]
    nombre_a_iso = {v["pais"]: k for k, v in por_pais.items()}
    listas = [
        "mayores_ganancias_absolutas", "menores_ganancias_absolutas", "mayores_ganancias_relativas_pct",
        "mayores_subidas_de_ranking", "mayores_caidas_de_ranking",
    ]
    isos = []
    for clave in listas:
        for x in g[clave]:
            if x["iso3"] not in isos:
                isos.append(x["iso3"])
    cruces = g["cruces_destacados"][:5]
    for c in cruces:
        for nombre in (c["adelantó"], c["a"]):
            iso = nombre_a_iso.get(nombre)
            if iso and iso not in isos:
                isos.append(iso)
    valles = sorted(
        g["paises_con_valle_posterior_al_inicio"],
        key=lambda x: x["valor_pico"] - x["valor_valle"],
        reverse=True,
    )[:6]
    caidas = sorted(g["anios_con_caida_mediana"], key=lambda x: x["mediana_cambio"])[:5]
    meta = {
        k: h["meta"][k]
        for k in ("nombre", "unidad", "anio_inicial", "anio_final", "anios_transcurridos",
                  "paises_incluidos", "limitaciones")
    }
    return {
        "meta": meta,
        "grupo": {**{c: g[c] for c in listas},
                  "dispersion_inicial": g["dispersion_inicial"],
                  "dispersion_final": g["dispersion_final"],
                  "cruces_destacados": cruces,
                  "paises_con_mayor_retroceso_posterior": valles,
                  "anios_con_caida_mediana": caidas},
        "paises_clave": {iso: por_pais[iso] for iso in isos},
    }


def _decimales(txt):
    return len(txt.split(".")[1]) if "." in txt else 0


def _a_float(txt):
    try:
        return float(str(txt).strip().replace(",", "."))
    except ValueError:
        return None


def resolver_ruta(paquete, ruta):
    """Resuelve una ruta con puntos dentro del paquete de hechos (las listas se indexan desde 0).
    Devuelve el número (en valor absoluto), la longitud si es una lista, o None si no existe."""
    o = paquete
    for parte in str(ruta).split("."):
        if isinstance(o, dict) and parte in o:
            o = o[parte]
        elif isinstance(o, list) and parte.isdigit() and int(parte) < len(o):
            o = o[int(parte)]
        else:
            return None
    if isinstance(o, bool):
        return None
    if isinstance(o, list):
        return float(len(o))
    if isinstance(o, (int, float)):
        return abs(float(o))
    return None


def verificar_cifras(g, paquete):
    """Cada cifra declarada debe apuntar a una ruta real de los hechos con ese valor, y cada cifra
    escrita en el texto debe corresponder a una cifra declarada y verificada. Devuelve lista de errores."""
    errores, verificadas = [], []
    declaradas = list(g.get("cifras") or [])
    for e in g.get("escenas") or []:
        if isinstance(e, dict):
            declaradas += list(e.get("cifras") or [])
    for c in declaradas:
        if not isinstance(c, dict) or "valor" not in c or "ruta" not in c:
            errores.append("Cada elemento de 'cifras' debe ser {\"valor\": ..., \"ruta\": ...}.")
            continue
        txt = str(c["valor"]).strip().replace(",", ".")
        x = _a_float(txt)
        real = resolver_ruta(paquete, c["ruta"])
        if x is None:
            errores.append(f"La cifra declarada «{c['valor']}» no es un número.")
        elif real is None:
            errores.append(f"La ruta «{c['ruta']}» no existe en los hechos o no es un número.")
        elif abs(x - real) > 0.5 * 10 ** (-_decimales(txt)) + 1e-9:
            errores.append(f"La ruta «{c['ruta']}» vale {real:g}, no {c['valor']}.")
        else:
            verificadas.append(x)
    return errores, verificadas


# ----------------------------------------------------------------------------
# Validador
# ----------------------------------------------------------------------------
def validar(g, hechos, paquete, G):
    """Devuelve la lista de problemas del guion (vacía si es válido)."""
    if not isinstance(g, dict):
        return ["La respuesta no es un objeto JSON."]
    errores = []
    for k in ("titulo", "pregunta", "tesis"):
        if not isinstance(g.get(k), str) or not g[k].strip():
            errores.append(f"Falta el campo '{k}'.")
    escenas = g.get("escenas")
    if not isinstance(escenas, list) or not (G["escenas_min"] <= len(escenas) <= G["escenas_max"]):
        errores.append(f"'escenas' debe ser una lista de {G['escenas_min']} a {G['escenas_max']} elementos.")
        return errores

    nombres = {v["pais"] for v in hechos["por_pais"].values()}
    textos = [g.get("titulo") or "", g.get("pregunta") or "", g.get("tesis") or ""]
    textos_escenas = []
    for i, e in enumerate(escenas, 1):
        if not isinstance(e, dict):
            errores.append(f"La escena {i} no es un objeto.")
            continue
        if e.get("id") != i:
            errores.append(f"El id de la escena {i} debe ser {i}.")
        if e.get("visual") not in VISUALES:
            errores.append(f"Escena {i}: 'visual' debe ser uno de {VISUALES}.")
        foco = e.get("foco", [])
        if not isinstance(foco, list) or any(f not in nombres for f in foco):
            errores.append(f"Escena {i}: 'foco' debe contener nombres de países copiados EXACTAMENTE de los hechos.")
        t = e.get("texto")
        if not isinstance(t, str) or not t.strip():
            errores.append(f"Escena {i}: falta 'texto'.")
            textos_escenas.append("")
        else:
            textos_escenas.append(t)
    textos += textos_escenas
    completo = "\n".join(textos)

    # 1. Cifras: cada cifra del texto debe estar declarada con su ruta y verificada contra los hechos
    errores_cifras, verificadas = verificar_cifras(g, paquete)
    errores += errores_cifras
    sin_respaldo = []
    for crudo in re.findall(r"\d+(?:[.,]\d+)?", completo):
        norm = crudo.replace(",", ".")
        tol = 0.5 * 10 ** (-_decimales(norm)) + 1e-9
        if not any(abs(float(norm) - d) <= tol for d in verificadas) and crudo not in sin_respaldo:
            sin_respaldo.append(crudo)
    if sin_respaldo:
        errores.append("Cifras del texto SIN respaldo en 'cifras' (declárala con su ruta exacta en HECHOS, "
                       "o quítala del texto): " + ", ".join(sin_respaldo))

    # 2. Longitud
    palabras = sum(len(t.split()) for t in textos_escenas)
    if palabras < G["palabras_min"] or palabras > G["palabras_max"]:
        errores.append(f"El texto de las escenas tiene {palabras} palabras; debe estar entre "
                       f"{G['palabras_min']} y {G['palabras_max']}.")

    # 3. Tono
    if "!" in completo or "¡" in completo:
        errores.append("No uses signos de exclamación.")
    bajo = completo.lower()
    usadas = [p for p in PALABRAS_PROHIBIDAS if p in bajo]
    if usadas:
        errores.append("Palabras de tono sensacionalista prohibidas: " + ", ".join(usadas))

    # 4. Causalidad sin fuente
    for frase in re.split(r"(?<=[.!?])\s+", completo):
        f = frase.lower()
        if any(c in f for c in FRASES_CAUSALES) and not any(c in f for c in COBERTURAS):
            errores.append(f"Afirmación causal sin respaldo en los hechos (conviértela en hipótesis o quítala): «{frase.strip()[:140]}»")

    # 5. Apertura y cierre
    if textos_escenas and not re.search(r"\d", textos_escenas[0]):
        errores.append("La primera escena debe incluir un dato (cifra) sorprendente de los hechos.")
    if textos_escenas:
        ultimo = textos_escenas[-1]
        lu = ultimo.lower()
        anio = str(hechos["meta"]["anio_final"])
        if "banco mundial" not in lu:
            errores.append("La última escena debe nombrar la fuente: Banco Mundial.")
        if anio not in ultimo:
            errores.append(f"La última escena debe mencionar el último año de datos ({anio}).")
        if not any(k in lu for k in ("interpol", "estimaci", "modelad")):
            errores.append("La última escena debe declarar la limitación: cifras estimadas e interpoladas.")
    if re.search(r"(respald|avala|patrocin|en colaboración con|en alianza con)[^.]{0,40}banco mundial", bajo):
        errores.append("No sugieras que el Banco Mundial respalda o participa en el video.")
    return errores


# ----------------------------------------------------------------------------
# Proveedores de LLM (módulos intercambiables)
# ----------------------------------------------------------------------------
def _post(url, headers, cuerpo, reintentos=4):
    espera = 20
    for i in range(1, reintentos + 1):
        try:
            r = requests.post(url, headers=headers, json=cuerpo, timeout=180)
        except requests.RequestException as e:
            if i == reintentos:
                raise RuntimeError(f"Error de red: {e}") from e
            print(f"  error de red ({e}); reintento en {espera}s")
            time.sleep(espera)
            espera *= 2
            continue
        if r.status_code == 429 and "3036" in r.text:  # Cloudflare: cuota diaria agotada
            raise RuntimeError("Cloudflare: se agotó la cuota diaria gratuita de Neurons.")
        if r.status_code in (429, 500, 502, 503, 504) and i < reintentos:
            try:
                pausa = int(float(r.headers.get("Retry-After", espera)))
            except ValueError:
                pausa = espera
            print(f"  HTTP {r.status_code}; reintento en {pausa}s")
            time.sleep(pausa)
            espera *= 2
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:400]}")
        return r.json()


def llamar_gemini(sistema, usuario, c):
    clave = os.environ.get("GEMINI_API_KEY")
    if not clave:
        raise RuntimeError("Falta el secreto GEMINI_API_KEY.")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{c['modelo']}:generateContent"
    cuerpo = {
        "systemInstruction": {"parts": [{"text": sistema}]},
        "contents": [{"role": "user", "parts": [{"text": usuario}]}],
        "generationConfig": {
            "maxOutputTokens": c.get("max_tokens", 16000),
            "responseMimeType": "application/json",
        },
    }
    datos = _post(url, {"x-goog-api-key": clave, "Content-Type": "application/json"}, cuerpo)
    cand = (datos.get("candidates") or [None])[0]
    if not cand:
        raise RuntimeError(f"Gemini no devolvió candidatos: {str(datos.get('promptFeedback'))[:300]}")
    if cand.get("finishReason") == "MAX_TOKENS":
        raise RuntimeError("Gemini cortó la respuesta por longitud (MAX_TOKENS).")
    partes = (cand.get("content") or {}).get("parts") or []
    texto = "".join(p.get("text", "") for p in partes if not p.get("thought"))
    if not texto.strip():
        raise RuntimeError("Gemini devolvió una respuesta vacía.")
    return texto


def llamar_cloudflare(sistema, usuario, c):
    cuenta = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    if not cuenta or not token:
        raise RuntimeError("Faltan los secretos CLOUDFLARE_ACCOUNT_ID y/o CLOUDFLARE_API_TOKEN.")
    url = f"https://api.cloudflare.com/client/v4/accounts/{cuenta}/ai/run/{c['modelo']}"
    cuerpo = {
        "messages": [{"role": "system", "content": sistema}, {"role": "user", "content": usuario}],
        "max_tokens": c.get("max_tokens", 4096),
        "temperature": c.get("temperatura", 0.6),
    }
    datos = _post(url, {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, cuerpo)
    res = datos.get("result") or {}
    resp = res.get("response")
    if resp is None and res.get("choices"):  # formato tipo OpenAI en algunos modelos
        resp = res["choices"][0]["message"]["content"]
    if isinstance(resp, (dict, list)):
        resp = json.dumps(resp, ensure_ascii=False)
    if not resp or not str(resp).strip():
        raise RuntimeError(f"Cloudflare devolvió una respuesta vacía: {str(datos)[:300]}")
    return str(resp)


PROVEEDORES = {"gemini": llamar_gemini, "cloudflare": llamar_cloudflare}


def listar_modelos_gemini():
    clave = os.environ.get("GEMINI_API_KEY")
    if not clave:
        raise SystemExit("Define la variable de entorno GEMINI_API_KEY para listar los modelos.")
    r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                     headers={"x-goog-api-key": clave}, params={"pageSize": 200}, timeout=60)
    r.raise_for_status()
    for m in r.json().get("models", []):
        if "generateContent" in m.get("supportedGenerationMethods", []):
            print(m["name"].replace("models/", ""))


# ----------------------------------------------------------------------------
# Generación
# ----------------------------------------------------------------------------
DEFAULTS = {
    "proveedor": "gemini", "respaldo": None, "intentos_maximos": 3,
    "estructura": "pregunta_misterio_resolucion", "pregunta": "",
    "palabras_objetivo": "entre 900 y 1300", "palabras_min": 600, "palabras_max": 1600,
    "escenas_min": 7, "escenas_max": 14,
    "gemini": {"modelo": "gemini-3.1-flash-lite", "max_tokens": 16000},
    "cloudflare": {"modelo": "@cf/meta/llama-3.1-8b-instruct", "max_tokens": 6000, "temperatura": 0.6},
}


def config_guion(cfg):
    G = {**DEFAULTS, **(cfg.get("guion") or {})}
    for k in ("gemini", "cloudflare"):
        G[k] = {**DEFAULTS[k], **((cfg.get("guion") or {}).get(k) or {})}
    return G


def construir_usuario(G, paquete, estructura, errores_previos=None):
    esquema = {
        "titulo": "título honesto, en forma de pregunta o tesis",
        "pregunta": "la pregunta concreta que responde el video",
        "tesis": "la tesis propia que defienden los datos, en una o dos frases",
        "cifras": [{"valor": "1960", "ruta": "meta.anio_inicial"}],
        "escenas": [{"id": 1, "visual": "|".join(VISUALES), "foco": ["nombre exacto de país en los hechos"],
                     "texto": "locución de la escena (80 a 140 palabras)",
                     "cifras": [{"valor": "cifra como aparece en el texto", "ruta": "ruta.en.los.hechos"}]}],
    }
    partes = [
        f"PREGUNTA DEL VIDEO: {G['pregunta']}",
        f"ESTRUCTURA NARRATIVA: {ESTRUCTURAS[estructura]}",
        f"LONGITUD: {G['palabras_objetivo']} palabras en total sumando el texto de todas las escenas, "
        f"entre {G['escenas_min']} y {G['escenas_max']} escenas.",
        "HECHOS (única fuente permitida de cifras):\n" + json.dumps(paquete, ensure_ascii=False, separators=(",", ":")),
        "FORMATO DE SALIDA (JSON; los ids de escena van 1, 2, 3...):\n" + json.dumps(esquema, ensure_ascii=False, indent=1),
    ]
    if errores_previos:
        partes.append("TU INTENTO ANTERIOR FUE RECHAZADO. Corrige estos problemas y devuelve el JSON completo de nuevo:\n- "
                      + "\n- ".join(errores_previos))
    return "\n\n".join(partes)


def extraer_json(texto):
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", texto.strip(), flags=re.MULTILINE).strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j < 0:
        raise ValueError("La respuesta no contiene un objeto JSON.")
    return json.loads(t[i:j + 1])


def generar(G, hechos, paquete, estructura, proveedor):
    orden = [proveedor] + ([G["respaldo"]] if G.get("respaldo") and G["respaldo"] != proveedor else [])
    errores, ultimo = None, None
    for intento in range(1, G["intentos_maximos"] + 1):
        usuario = construir_usuario(G, paquete, estructura, errores)
        texto, usado = None, None
        for prov in orden:
            try:
                print(f"Intento {intento}/{G['intentos_maximos']} con {prov} ({G[prov]['modelo']})...")
                texto = PROVEEDORES[prov](SISTEMA, usuario, G[prov])
                usado = prov
                break
            except Exception as e:  # noqa: BLE001
                print(f"  {prov} falló: {e}")
        if texto is None:
            raise SystemExit("Ningún proveedor de LLM respondió. Revisa los secretos y las cuotas.")
        try:
            guion = extraer_json(texto)
        except ValueError as e:
            errores, ultimo = [f"JSON inválido: {e}"], {"intento": intento, "respuesta": texto[:3000]}
            print("  rechazado: JSON inválido")
            continue
        errores = validar(guion, hechos, paquete, G)
        if not errores:
            return guion, {"proveedor": usado, "modelo": G[usado]["modelo"], "intentos": intento}
        print("  rechazado:")
        for e in errores:
            print("   -", e)
        ultimo = {"intento": intento, "guion": guion, "errores": errores}
    (DATA / "guion_rechazado.json").write_text(json.dumps(ultimo, ensure_ascii=False, indent=1), encoding="utf-8")
    raise SystemExit(f"No se logró un guion válido en {G['intentos_maximos']} intentos. "
                     "Último intento guardado en data/guion_rechazado.json")


def guardar(guion, info, estructura, hechos):
    palabras = sum(len(e["texto"].split()) for e in guion["escenas"])
    salida = {
        "meta": {**info, "estructura": estructura, "palabras": palabras,
                 "duracion_estimada_min": round(palabras / 150, 1),  # supuesto: ~150 palabras por minuto
                 "generado_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "indicador": hechos["meta"]["indicador"], "anio_final": hechos["meta"]["anio_final"]},
        "guion": guion,
    }
    (DATA / "guion.json").write_text(json.dumps(salida, ensure_ascii=False, indent=1), encoding="utf-8")
    lineas = [guion["titulo"], "", "Pregunta: " + guion["pregunta"], "Tesis: " + guion["tesis"], ""]
    for e in guion["escenas"]:
        lineas += [f"[{e['id']}] {e['visual']} · {', '.join(e.get('foco') or []) or '—'}", e["texto"], ""]
    (DATA / "guion.txt").write_text("\n".join(lineas), encoding="utf-8")
    print(f"\nGuion válido: {palabras} palabras (~{salida['meta']['duracion_estimada_min']} min). "
          "Guardado en data/guion.json y data/guion.txt")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--proveedor", choices=list(PROVEEDORES))
    ap.add_argument("--estructura", choices=list(ESTRUCTURAS))
    ap.add_argument("--validar", metavar="ARCHIVO", help="revalida un guion.json existente")
    ap.add_argument("--listar-modelos", action="store_true", help="lista modelos de Gemini disponibles")
    args = ap.parse_args()

    if args.listar_modelos:
        listar_modelos_gemini()
        return
    cfg = cargar_config()
    G = config_guion(cfg)
    hechos = json.loads((DATA / "hechos.json").read_text(encoding="utf-8"))
    paquete = paquete_hechos(hechos)

    if args.validar:
        guion = json.loads(open(args.validar, encoding="utf-8").read()).get("guion")
        errores = validar(guion, hechos, paquete, G)
        print("Guion válido." if not errores else "Problemas:\n- " + "\n- ".join(errores))
        sys.exit(1 if errores else 0)

    estructura = args.estructura or G["estructura"]
    proveedor = args.proveedor or G["proveedor"]
    guion, info = generar(G, hechos, paquete, estructura, proveedor)
    guardar(guion, info, estructura, hechos)


if __name__ == "__main__":
    main()
