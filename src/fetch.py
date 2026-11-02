#!/usr/bin/env python3
"""Descarga datos del Banco Mundial (API v2) con reintentos y caché.

Uso (desde la raíz del repositorio):
    python src/fetch.py

Genera:
    data/paises.json            países reales (sin agregados) con región y nivel de ingreso
    data/<CODIGO>.json          serie del indicador, solo países reales y valores no nulos
"""
import json
import pathlib
import time
from datetime import datetime, timezone

import requests
import yaml

API = "https://api.worldbank.org/v2"
RAIZ = pathlib.Path(__file__).resolve().parent.parent
DATA = RAIZ / "data"


def cargar_config(ruta=None):
    ruta = pathlib.Path(ruta) if ruta else RAIZ / "config" / "canal.yaml"
    with open(ruta, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _get(url, params, reintentos=5):
    """GET con reintentos y espera exponencial. Devuelve el JSON de la API."""
    espera = 2
    ultimo_error = None
    for intento in range(1, reintentos + 1):
        try:
            r = requests.get(url, params=params, timeout=60)
            r.raise_for_status()
            datos = r.json()
            # La API responde 200 con un mensaje de error en lugar de datos.
            if not isinstance(datos, list) or len(datos) < 2:
                raise RuntimeError(f"Respuesta inesperada de la API: {str(datos)[:300]}")
            return datos
        except Exception as e:  # noqa: BLE001
            ultimo_error = e
            print(f"  intento {intento}/{reintentos} falló: {e}")
            if intento < reintentos:
                time.sleep(espera)
                espera *= 2
    raise RuntimeError(f"No se pudo obtener {url}: {ultimo_error}")


def _paginar(url, params):
    """Recorre todas las páginas. Devuelve (meta de la primera página, filas)."""
    params = dict(params, format="json", page=1)
    meta, filas = _get(url, params)
    filas = list(filas or [])
    for pagina in range(2, int(meta.get("pages", 1)) + 1):
        params["page"] = pagina
        _, mas = _get(url, params)
        filas.extend(mas or [])
    return meta, filas


def _cache_vigente(ruta, dias):
    if not ruta.exists():
        return False
    edad_s = time.time() - ruta.stat().st_mtime
    return edad_s < dias * 86400


def descargar_paises(cfg):
    ruta = DATA / "paises.json"
    if _cache_vigente(ruta, cfg["datos"]["cache_dias"]):
        print("paises.json: usando caché")
        return json.loads(ruta.read_text(encoding="utf-8"))
    print("Descargando metadatos de países...")
    _, filas = _paginar(f"{API}/country", {"per_page": 400})
    paises = {}
    for p in filas:
        # Los agregados (mundo, regiones, grupos de ingreso) tienen región "NA".
        if p["region"]["id"] == "NA":
            continue
        paises[p["id"]] = {
            "iso3": p["id"],
            "nombre": p["name"],
            "region_id": p["region"]["id"],
            "region": p["region"]["value"].strip(),
            "ingreso_id": p["incomeLevel"]["id"],
            "ingreso": p["incomeLevel"]["value"].strip(),
        }
    ruta.write_text(json.dumps(paises, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  {len(paises)} países (agregados excluidos)")
    return paises


def descargar_indicador(cfg, paises):
    codigo = cfg["indicador"]["codigo"]
    ruta = DATA / f"{codigo}.json"
    if _cache_vigente(ruta, cfg["datos"]["cache_dias"]):
        print(f"{codigo}.json: usando caché")
        return json.loads(ruta.read_text(encoding="utf-8"))
    d = cfg["datos"]
    print(f"Descargando {codigo} ({d['anio_inicio']}-{d['anio_fin_maximo']})...")
    meta, filas = _paginar(
        f"{API}/country/all/indicator/{codigo}",
        {"per_page": 20000, "date": f"{d['anio_inicio']}:{d['anio_fin_maximo']}"},
    )
    registros = []
    for f in filas:
        iso3 = f.get("countryiso3code")
        if iso3 not in paises or f.get("value") is None:
            continue
        registros.append(
            {"iso3": iso3, "pais": paises[iso3]["nombre"], "anio": int(f["date"]), "valor": f["value"]}
        )
    salida = {
        "indicador": codigo,
        "nombre_fuente": f["indicator"]["value"] if filas else cfg["indicador"]["nombre_fuente"],
        "ultima_actualizacion_api": meta.get("lastupdated"),
        "descargado_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "filas": registros,
    }
    ruta.write_text(json.dumps(salida, ensure_ascii=False), encoding="utf-8")
    print(f"  {len(registros)} observaciones guardadas")
    return salida


def main():
    DATA.mkdir(exist_ok=True)
    cfg = cargar_config()
    paises = descargar_paises(cfg)
    descargar_indicador(cfg, paises)
    print("Listo.")


if __name__ == "__main__":
    main()
