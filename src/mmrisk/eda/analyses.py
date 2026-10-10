"""Analyses behind the EDA report. Each function answers one question and returns plain data.

Label-related analyses use the *matured* population (transactions old enough that their
labels are final) unless stated otherwise. `truth_audit` is the only function that reads
information a real analyst would not have, and it is skipped when ground truth is absent.
"""

from __future__ import annotations

import polars as pl

from mmrisk.eda.data import SIGNALS, Analysis

M = "matured"  # SQL filter for the matured population
ANY_SIGNAL = " OR ".join(SIGNALS)
SIGNAL_COUNT = " + ".join(f"{s}::INT" for s in SIGNALS)
DELAY_DAYS = "date_diff('second', ts, label_at) / 86400.0"


def overview(an: Analysis) -> dict:
    row = an.query(
        """SELECT count(*) AS n, count(DISTINCT customer_id) AS customers,
                  min(ts) AS first_ts, max(ts) AS last_ts,
                  sum(matured::INT) AS matured_n FROM a"""
    ).row(0, named=True)
    row["cutoff"] = an.cutoff
    row["matured_cutoff"] = an.matured_cutoff
    return row


def label_quality(an: Analysis) -> dict:
    """Class balance on the matured population, from the observed labels only."""
    r = an.query(
        f"""SELECT count(*) AS n, sum(final_label)::INT AS fraud,
                   sum(final_label)::DOUBLE / count(*) AS rate
            FROM a WHERE {M}"""
    ).row(0, named=True)
    r["imbalance_ratio"] = (r["n"] - r["fraud"]) / max(r["fraud"], 1)
    return r


def label_delays(an: Analysis) -> pl.DataFrame:
    """Days from transaction to fraud label, one row per whole day."""
    return an.query(
        f"""SELECT floor({DELAY_DAYS})::INT AS days, count(*) AS n
            FROM a WHERE final_label = 1 AND {M} GROUP BY 1 ORDER BY 1"""
    )


def delay_quantiles(an: Analysis) -> dict:
    return an.query(
        f"""SELECT quantile_cont({DELAY_DAYS}, 0.5) AS median_days,
                   quantile_cont({DELAY_DAYS}, 0.9) AS p90_days,
                   max({DELAY_DAYS}) AS max_days
            FROM a WHERE final_label = 1 AND {M}"""
    ).row(0, named=True)


def maturity_curve(an: Analysis) -> pl.DataFrame:
    """Weekly fraud rate: labels known at the cutoff versus every label that eventually arrives."""
    return an.query(
        """SELECT date_trunc('week', ts)::DATE AS week, count(*) AS n,
                  coalesce(sum(CASE WHEN label_known THEN final_label ELSE 0 END), 0)::DOUBLE
                      / count(*) AS known_rate,
                  coalesce(sum(final_label), 0)::DOUBLE / count(*) AS eventual_rate
           FROM a GROUP BY 1 ORDER BY 1"""
    )


def recent_label_gap(an: Analysis) -> dict:
    """How much fraud is still invisible in the final 14 days of data."""
    return an.query(
        f"""SELECT coalesce(sum(CASE WHEN label_known THEN final_label ELSE 0 END), 0)::INT
                       AS known,
                   coalesce(sum(final_label), 0)::INT AS eventual
            FROM a WHERE ts > TIMESTAMP '{an.cutoff.isoformat(sep=" ")}' - INTERVAL 14 DAY"""
    ).row(0, named=True)


def volume_by_day_of_month(an: Analysis) -> pl.DataFrame:
    return an.query(
        """SELECT dom, avg(n) AS mean_daily FROM
             (SELECT day, any_value(dom) AS dom, count(*) AS n FROM a GROUP BY day)
           GROUP BY dom ORDER BY dom"""
    )


def volume_by_weekday(an: Analysis) -> pl.DataFrame:
    return an.query(
        """SELECT dow, avg(n) AS mean_daily FROM
             (SELECT day, any_value(dow) AS dow, count(*) AS n FROM a GROUP BY day)
           GROUP BY dow ORDER BY dow"""
    )


def salary_window_effect(an: Analysis) -> dict:
    return an.query(
        """SELECT avg(n) FILTER (WHERE dom >= 25 OR dom <= 3) AS salary_mean,
                  avg(n) FILTER (WHERE dom BETWEEN 4 AND 24) AS other_mean
           FROM (SELECT day, any_value(dom) AS dom, count(*) AS n FROM a GROUP BY day)"""
    ).row(0, named=True)


def hourly_shares(an: Analysis) -> pl.DataFrame:
    """Share of each class's transactions in each hour of the day."""
    return an.query(
        f"""SELECT hr, final_label AS label, count(*)::DOUBLE /
                   sum(count(*)) OVER (PARTITION BY final_label) AS share
            FROM a WHERE {M} GROUP BY hr, final_label ORDER BY label, hr"""
    )


def night_share(an: Analysis) -> dict:
    return an.query(
        f"""SELECT avg(f_night::INT) FILTER (WHERE final_label = 1) AS fraud_night,
                   avg(f_night::INT) FILTER (WHERE final_label = 0) AS legit_night
            FROM a WHERE {M}"""
    ).row(0, named=True)


def amount_hist(an: Analysis, column: str = "amount", step: float = 0.1) -> pl.DataFrame:
    """Share of each class per log10 bucket of an amount-like column."""
    return an.query(
        f"""SELECT floor(log10({column}) / {step}) * {step} AS bucket, final_label AS label,
                   count(*)::DOUBLE / sum(count(*)) OVER (PARTITION BY final_label) AS share
            FROM a WHERE {M} AND {column} > 0 GROUP BY 1, 2 ORDER BY 2, 1"""
    )


def amount_summary(an: Analysis) -> dict:
    return an.query(
        f"""SELECT median(amount) FILTER (WHERE final_label = 1) AS fraud_median,
                   median(amount) FILTER (WHERE final_label = 0) AS legit_median,
                   median(rel_amount) FILTER (WHERE final_label = 1) AS fraud_rel_median,
                   median(rel_amount) FILTER (WHERE final_label = 0) AS legit_rel_median
            FROM a WHERE {M}"""
    ).row(0, named=True)


def amount_by_type(an: Analysis) -> pl.DataFrame:
    return an.query(
        f"""SELECT ttype AS transaction_type, count(*) AS txns, median(amount) AS median_kes,
                   quantile_cont(amount, 0.99) AS p99_kes,
                   sum(final_label)::DOUBLE / count(*) AS fraud_rate
            FROM a WHERE {M} GROUP BY 1 ORDER BY txns DESC"""
    )


def dimension_table(an: Analysis) -> pl.DataFrame:
    """Fraud rate and lift for each value of the main categorical columns."""
    base = an.scalar(f"SELECT avg(final_label) FROM a WHERE {M}")
    parts = []
    for label, expr in (
        ("transaction type", "ttype"),
        ("channel", "channel"),
        ("segment", "segment"),
        ("counterparty type", "coalesce(cp_type, 'NONE')"),
        ("county", "county"),
    ):
        parts.append(
            an.query(
                f"""SELECT '{label}' AS dimension, {expr} AS value, count(*) AS txns,
                           sum(final_label)::INT AS fraud, avg(final_label) AS rate
                    FROM a WHERE {M} GROUP BY 2 ORDER BY txns DESC"""
            )
        )
    return pl.concat(parts).with_columns((pl.col("rate") / base).alias("lift"))


def customer_stats(an: Analysis) -> dict:
    per = """(SELECT customer_id, any_value(segment) AS segment, count(*) AS n,
                     sum(final_label) FILTER (WHERE matured) AS fraud
              FROM a GROUP BY 1)"""
    spread = an.query(
        f"""SELECT segment, count(*) AS customers, quantile_cont(n, 0.5) AS p50,
                   quantile_cont(n, 0.9) AS p90, quantile_cont(n, 0.99) AS p99, max(n) AS max
            FROM {per} GROUP BY 1 ORDER BY 1"""
    )
    top10 = an.scalar(
        f"""SELECT sum(n) FILTER (WHERE rk <= 0.1 * cnt)::DOUBLE / sum(n) FROM
              (SELECT n, row_number() OVER (ORDER BY n DESC) AS rk, count(*) OVER () AS cnt
               FROM {per})"""
    )
    victims = an.query(
        f"""SELECT count(*) FILTER (WHERE fraud > 0) AS victims, count(*) AS customers,
                   sum(fraud) FILTER (WHERE fraud > 1)::DOUBLE
                       / nullif(sum(fraud), 0) AS repeat_share,
                   max(fraud) AS max_fraud
            FROM {per}"""
    ).row(0, named=True)
    return {"spread": spread, "top10_share": top10, "victims": victims}


def _top10_share(an: Analysis, cp_type: str) -> float | None:
    return an.scalar(
        f"""SELECT sum(n) FILTER (WHERE rk <= 10)::DOUBLE / sum(n) FROM
              (SELECT count(*) AS n, row_number() OVER (ORDER BY count(*) DESC) AS rk
               FROM a WHERE cp_type = '{cp_type}' GROUP BY cp_id)"""
    )


def counterparty_stats(an: Analysis) -> dict:
    out = {
        "merchant_top10_share": _top10_share(an, "MERCHANT"),
        "agent_top10_share": _top10_share(an, "AGENT"),
        "fraud_share_by_cp": an.query(
            f"""SELECT coalesce(cp_type, 'NONE') AS counterparty_type,
                       sum(final_label)::DOUBLE / sum(sum(final_label)) OVER () AS share_of_fraud
                FROM a WHERE {M} GROUP BY 1 ORDER BY 2 DESC"""
        ),
    }
    if an.has_merchants:
        out["categories"] = an.query(
            f"""SELECT m.merchant_type, m.category, count(*) AS txns,
                       median(a.amount) AS median_kes,
                       sum(a.final_label)::DOUBLE / count(*) AS fraud_rate
                FROM a JOIN merch m ON m.merchant_id = a.cp_id
                WHERE a.{M} GROUP BY 1, 2 ORDER BY txns DESC"""
        )
    return out


def signal_table(an: Analysis) -> pl.DataFrame:
    """Prevalence, precision (fraud rate when present), lift and recall of each signal."""
    base = an.scalar(f"SELECT avg(final_label) FROM a WHERE {M}")
    total_fraud = an.scalar(f"SELECT sum(final_label) FROM a WHERE {M}")
    rows = []
    for col, desc in {**SIGNALS, "any_signal": "Any of the signals above"}.items():
        expr = f"({ANY_SIGNAL})" if col == "any_signal" else col
        r = an.query(
            f"""SELECT count(*) FILTER (WHERE {expr}) AS present,
                       sum(final_label) FILTER (WHERE {expr}) AS fraud_present,
                       count(*) AS n FROM a WHERE {M}"""
        ).row(0, named=True)
        present, fraud_present = r["present"], r["fraud_present"] or 0
        precision = fraud_present / present if present else 0.0
        rows.append(
            {
                "signal": col,
                "description": desc,
                "prevalence": present / r["n"],
                "precision": precision,
                "lift": precision / base if base else 0.0,
                "recall": fraud_present / total_fraud if total_fraud else 0.0,
            }
        )
    return pl.DataFrame(rows)


CORR_COLUMNS = {
    "log_amount": "ln(amount)",
    "log_rel_amount": "ln(rel_amount)",
    "log_gap": "ln(1 + secs_since_prev)",
    "n_10min": "n_10min::DOUBLE",
    "n_1h": "n_1h::DOUBLE",
    "n_24h": "n_24h::DOUBLE",
    "night": "f_night::DOUBLE",
    "new_device": "f_new_device::DOUBLE",
    "new_recipient": "f_new_recipient::DOUBLE",
    "location_jump": "f_location_jump::DOUBLE",
    "fraud_label": "final_label::DOUBLE",
}


def corr_inputs(an: Analysis) -> pl.DataFrame:
    select = ", ".join(f"{expr} AS {name}" for name, expr in CORR_COLUMNS.items())
    return an.query(f"SELECT {select} FROM a WHERE {M}")


def spearman_matrix(df: pl.DataFrame, columns: list[str]) -> pl.DataFrame:
    """Pairwise-complete Spearman correlation (Pearson correlation of average ranks)."""
    rows = []
    for a in columns:
        row: dict = {"feature": a}
        for b in columns:
            if a == b:
                row[b] = 1.0
                continue
            pair = df.select(a, b).drop_nulls()
            pair = pair.filter(pl.col(a).is_finite() & pl.col(b).is_finite())
            if pair.height < 3 or pair[a].n_unique() < 2 or pair[b].n_unique() < 2:
                row[b] = None
                continue
            ranked = pair.select(
                pl.col(a).rank("average").alias(a), pl.col(b).rank("average").alias(b)
            )
            row[b] = float(ranked.select(pl.corr(a, b)).item())
        rows.append(row)
    return pl.DataFrame(rows)


def truth_audit(an: Analysis) -> dict | None:
    """Uses ground truth, which a real analyst would not have. None when unavailable."""
    if not an.truth_available:
        return None
    labels = an.query(
        f"""SELECT sum(t.true_is_fraud::INT) AS true_fraud,
                   sum((t.true_is_fraud AND a.final_label = 1)::INT) AS reported,
                   sum((NOT t.true_is_fraud AND a.final_label = 1)::INT) AS false_reports,
                   sum(a.final_label)::INT AS labelled_positive
            FROM a JOIN truth t USING (transaction_id) WHERE a.{M}"""
    ).row(0, named=True)
    patterns = an.query(
        f"""SELECT t.fraud_pattern AS pattern, count(*) AS rows,
                   avg((a.final_label = 1)::INT) AS reported_share,
                   avg(({ANY_SIGNAL})::INT) AS any_signal_share,
                   avg(({SIGNAL_COUNT})::DOUBLE) AS mean_signals
            FROM a JOIN truth t USING (transaction_id)
            WHERE a.{M} AND t.true_is_fraud GROUP BY 1 ORDER BY any_signal_share"""
    )
    return {"labels": labels, "patterns": patterns}
