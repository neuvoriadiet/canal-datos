#!/usr/bin/env python3
"""Motor de hechos: calcula en código todas las cifras que podrá usar el guion.

Uso (desde la raíz del repositorio, después de fetch.py):
    python src/facts.py

Lee data/paises.json y data/<CODIGO>.json; escribe data/hechos.json.
"""
import json
import statistics
from itertools import combinations

from fetch import DATA, cargar_config


def _signo(x):
    return (x > 0) - (x < 0)


def calcular_hechos(cfg, paises, indicador):
    d = cfg["datos"]
    region = d.get("region")
    objetivo = {k: v for k, v in paises.items() if region is None or v["region_id"] == region}

    # Series por país: {iso3: {anio: valor}}
    series = {}
    for f in indicador["filas"]:
        if f["iso3"] in objetivo:
            series.setdefault(f["iso3"], {})[f["anio"]] = f["valor"]

    n_obj = len(objetivo)
    anio_ini = d["anio_inicio"]

    def cobertura(anio):
        return sum(1 for s in series.values() if anio in s) / n_obj

    if cobertura(anio_ini) < d["cobertura_minima"]:
        raise SystemExit(f"El año inicial {anio_ini} no tiene cobertura suficiente ({cobertura(anio_ini):.0%}).")
    anio_fin = next(
        (a for a in range(d["anio_fin_maximo"], anio_ini, -1) if cobertura(a) >= d["cobertura_minima"]),
        None,
    )
    if anio_fin is None:
        raise SystemExit("No hay ningún año final con cobertura suficiente.")
    n_anios = anio_fin - anio_ini

    # Filtrado de países: deben tener ambos extremos y una serie casi completa.
    incluidos, excluidos = {}, []
    for iso3, s in series.items():
        presentes = sum(1 for a in range(anio_ini, anio_fin + 1) if a in s)
        completitud = presentes / (n_anios + 1)
        if anio_ini in s and anio_fin in s and completitud >= d["completitud_serie"]:
            incluidos[iso3] = s
        else:
            excluidos.append(objetivo[iso3]["nombre"])
    excluidos += [v["nombre"] for k, v in objetivo.items() if k not in series]

    # Rankings (1 = valor más alto)
    def ranking(anio):
        orden = sorted(incluidos, key=lambda k: incluidos[k][anio], reverse=True)
        return {k: i + 1 for i, k in enumerate(orden)}

    rank_ini, rank_fin = ranking(anio_ini), ranking(anio_fin)

    por_pais = {}
    for iso3, s in incluidos.items():
        v0, v1 = s[anio_ini], s[anio_fin]
        anios = sorted(s)
        pico = max(anios, key=lambda a: s[a])
        valle = min(anios, key=lambda a: s[a])
        cambios = [(a, s[a] - s[a - 1]) for a in anios if a - 1 in s and anio_ini < a <= anio_fin]
        mayor_subida = max(cambios, key=lambda c: c[1])
        mayor_caida = min(cambios, key=lambda c: c[1])
        cagr = ((v1 / v0) ** (1 / n_anios) - 1) * 100 if v0 > 0 and v1 > 0 else None
        por_pais[iso3] = {
            "pais": objetivo[iso3]["nombre"],
            "valor_inicial": round(v0, 2),
            "valor_final": round(v1, 2),
            "cambio_absoluto": round(v1 - v0, 2),
            "cambio_relativo_pct": round((v1 / v0 - 1) * 100, 1),
            "tcac_pct": round(cagr, 2) if cagr is not None else None,
            "anio_pico": pico,
            "valor_pico": round(s[pico], 2),
            "anio_valle": valle,
            "valor_valle": round(s[valle], 2),
            "mayor_subida_anual": {"anio": mayor_subida[0], "cambio": round(mayor_subida[1], 2)},
            "mayor_caida_anual": {"anio": mayor_caida[0], "cambio": round(mayor_caida[1], 2)},
            "ranking_inicial": rank_ini[iso3],
            "ranking_final": rank_fin[iso3],
            "cambio_de_ranking": rank_ini[iso3] - rank_fin[iso3],  # positivo = subió puestos
        }

    def top(clave, n=5, reverso=True):
        orden = sorted(por_pais, key=lambda k: por_pais[k][clave], reverse=reverso)[:n]
        return [{"iso3": k, "pais": por_pais[k]["pais"], clave: por_pais[k][clave]} for k in orden]

    # Cruces: parejas cuyo orden final es distinto del inicial.
    cruces = []
    for a, b in combinations(incluidos, 2):
        sa, sb = incluidos[a], incluidos[b]
        d0 = sa[anio_ini] - sb[anio_ini]
        d1 = sa[anio_fin] - sb[anio_fin]
        if _signo(d0) * _signo(d1) < 0:
            anio_perm = anio_fin
            for y in range(anio_fin, anio_ini - 1, -1):
                if y in sa and y in sb and _signo(sa[y] - sb[y]) == _signo(d1):
                    anio_perm = y
                else:
                    break
            lider, rezago = (a, b) if d1 > 0 else (b, a)
            cruces.append(
                {
                    "adelantó": objetivo[lider]["nombre"],
                    "a": objetivo[rezago]["nombre"],
                    "anio_adelanto_definitivo": anio_perm,
                    "brecha_inicial": round(abs(d0), 2),
                    "brecha_final": round(abs(d1), 2),
                }
            )
    cruces.sort(key=lambda c: c["brecha_inicial"], reverse=True)

    # Dispersión entre países
    def dispersion(anio):
        vals = {k: incluidos[k][anio] for k in incluidos}
        mx, mn = max(vals, key=vals.get), min(vals, key=vals.get)
        return {
            "anio": anio,
            "maximo": {"pais": objetivo[mx]["nombre"], "valor": round(vals[mx], 2)},
            "minimo": {"pais": objetivo[mn]["nombre"], "valor": round(vals[mn], 2)},
            "rango": round(vals[mx] - vals[mn], 2),
            "desviacion_estandar": round(statistics.pstdev(vals.values()), 2),
            "mediana": round(statistics.median(vals.values()), 2),
        }

    # Años en que la mediana del cambio anual fue negativa (caídas de la región)
    anios_con_caida = []
    for y in range(anio_ini + 1, anio_fin + 1):
        difs = [s[y] - s[y - 1] for s in incluidos.values() if y in s and y - 1 in s]
        if difs and statistics.median(difs) < 0:
            anios_con_caida.append({"anio": y, "mediana_cambio": round(statistics.median(difs), 2)})

    paises_con_caida_reciente = sorted(
        (
            {"pais": por_pais[k]["pais"], "anio_valle": por_pais[k]["anio_valle"],
             "valor_valle": por_pais[k]["valor_valle"], "valor_pico": por_pais[k]["valor_pico"],
             "anio_pico": por_pais[k]["anio_pico"]}
            for k in por_pais if por_pais[k]["anio_valle"] > anio_ini + 1
        ),
        key=lambda x: x["pais"],
    )

    return {
        "meta": {
            "indicador": cfg["indicador"]["codigo"],
            "nombre": cfg["indicador"]["nombre"],
            "unidad": cfg["indicador"]["unidad"],
            "fuente_atribucion": cfg["atribucion"],
            "licencia": cfg["licencia"],
            "region": region,
            "anio_inicial": anio_ini,
            "anio_final": anio_fin,
            "anios_transcurridos": n_anios,
            "paises_incluidos": len(incluidos),
            "paises_excluidos": sorted(excluidos),
            "ultima_actualizacion_api": indicador.get("ultima_actualizacion_api"),
            "limitaciones": [
                f"El último año con datos suficientes es {anio_fin}; hay un rezago de uno o dos años.",
                "Las series anuales provienen de las estimaciones de población de la ONU y están interpoladas entre años.",
                "Los valores son estimaciones modeladas, no conteos exactos.",
            ],
        },
        "grupo": {
            "mayores_ganancias_absolutas": top("cambio_absoluto", 5),
            "menores_ganancias_absolutas": top("cambio_absoluto", 5, reverso=False),
            "mayores_ganancias_relativas_pct": top("cambio_relativo_pct", 5),
            "mayores_subidas_de_ranking": top("cambio_de_ranking", 5),
            "mayores_caidas_de_ranking": top("cambio_de_ranking", 5, reverso=False),
            "dispersion_inicial": dispersion(anio_ini),
            "dispersion_final": dispersion(anio_fin),
            "cruces_destacados": cruces[:15],
            "total_cruces": len(cruces),
            "anios_con_caida_mediana": anios_con_caida,
            "paises_con_valle_posterior_al_inicio": paises_con_caida_reciente,
        },
        "por_pais": por_pais,
    }


def main():
    cfg = cargar_config()
    paises = json.loads((DATA / "paises.json").read_text(encoding="utf-8"))
    indicador = json.loads((DATA / f"{cfg['indicador']['codigo']}.json").read_text(encoding="utf-8"))
    hechos = calcular_hechos(cfg, paises, indicador)
    (DATA / "hechos.json").write_text(json.dumps(hechos, ensure_ascii=False, indent=1), encoding="utf-8")
    m, g = hechos["meta"], hechos["grupo"]
    print(f"{m['paises_incluidos']} países, {m['anio_inicial']}–{m['anio_final']}. Excluidos: {len(m['paises_excluidos'])}")
    print("Mayores ganancias absolutas:")
    for x in g["mayores_ganancias_absolutas"]:
        print(f"  {x['pais']}: +{x['cambio_absoluto']} {m['unidad']}")
    print("Guardado en data/hechos.json")


if __name__ == "__main__":
    main()
