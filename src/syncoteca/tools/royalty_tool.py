from decimal import Decimal, ROUND_HALF_UP
from crewai.tools import BaseTool


VAT_RATE_2026 = Decimal("0.22")


def _d(value: float | str) -> Decimal:
    return Decimal(str(value))


def _round(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), ROUND_HALF_UP)


class RoyaltyCalculatorTool(BaseTool):
    """Calculate royalties, taxes and margin for sync deals (RU tax rules 2026+)."""

    name: str = "royalty_calculator"
    description: str = (
        "Calculate income, author payouts, margin and VAT for a sync deal. "
        "Input: "
        "income (float, deal amount — specify whether it includes VAT via income_includes_vat bool), "
        "income_includes_vat (bool, default True — if True, income is the amount с НДС), "
        "output_vat_rate (float, default 22.0 for domestic RU; 0.0 for international), "
        "payees (list of dicts): "
        "  [{name, role, amount, payee_type, vat_rate, usn_rate, ndfl_rate}] "
        "  payee_type: 'individual' (физлицо, НДФЛ 13%), "
        "              'company_vat' (юрлицо/ИП с НДС), "
        "              'ip_no_vat' (ИП без НДС), "
        "              'ip_mixed' (ИП с УСН + НДФЛ). "
        "  For 'individual': amount = net 'на руки', ndfl_rate default 13.0. "
        "  For 'company_vat': amount = contract base без НДС, vat_rate required (e.g. 22.0 or 7.0). "
        "  For 'ip_no_vat': amount = contract amount. "
        "  For 'ip_mixed': amount = target net 'на руки', usn_rate and ndfl_rate required."
    )

    def _run(
        self,
        income: float,
        income_includes_vat: bool = True,
        output_vat_rate: float = 22.0,
        payees: list | None = None,
    ) -> str:
        income_d = _d(income)
        out_vat_pct = _d(output_vat_rate) / 100

        if income_includes_vat:
            income_ex_vat = _round(income_d / (1 + out_vat_pct))
            output_vat = _round(income_d - income_ex_vat)
            income_incl_vat = income_d
        else:
            income_ex_vat = _round(income_d)
            output_vat = _round(income_d * out_vat_pct)
            income_incl_vat = income_d + output_vat

        payee_rows = []
        total_author_incl = Decimal("0")
        total_author_ex = Decimal("0")
        total_input_vat = Decimal("0")

        for p in (payees or []):
            name = p.get("name", "?")
            role = p.get("role", "")
            amount = _d(p.get("amount", 0))
            ptype = p.get("payee_type", "ip_no_vat")

            if ptype == "individual":
                ndfl_pct = _d(p.get("ndfl_rate", 13.0)) / 100
                gross = _round(amount / (1 - ndfl_pct))
                ndfl = _round(gross * ndfl_pct)
                author_ex = amount
                author_incl = gross
                input_vat = Decimal("0")
                note = f"физлицо НДФЛ {p.get('ndfl_rate', 13)}%: начисл. {gross:,.2f}, НДФЛ {ndfl:,.2f}, на руки {amount:,.2f}"

            elif ptype == "company_vat":
                vat_pct = _d(p.get("vat_rate", 22.0)) / 100
                author_ex = amount
                vat_amt = _round(amount * vat_pct)
                author_incl = amount + vat_amt
                input_vat = vat_amt
                note = f"НДС {p.get('vat_rate', 22)}% к зачёту: {vat_amt:,.2f}"

            elif ptype == "ip_mixed":
                usn_pct = _d(p.get("usn_rate", 6.0)) / 100
                ndfl_pct = _d(p.get("ndfl_rate", 5.0)) / 100
                base = _round(amount / (1 - usn_pct))
                ndfl_amt = _round(base * ndfl_pct / (1 + ndfl_pct))
                author_ex = amount
                author_incl = base
                input_vat = Decimal("0")
                usn_amt = _round(base * usn_pct)
                note = f"ИП смешанный УСН {p.get('usn_rate')}%+НДФЛ {p.get('ndfl_rate')}%: база {base:,.2f}, УСН {usn_amt:,.2f}, НДФЛ {ndfl_amt:,.2f}"

            else:
                author_ex = amount
                author_incl = amount
                input_vat = Decimal("0")
                note = "ИП/юрлицо без НДС"

            payee_rows.append((name, role, author_ex, author_incl, input_vat, note))
            total_author_ex += author_ex
            total_author_incl += author_incl
            total_input_vat += input_vat

        margin = _round(income_ex_vat - total_author_incl + total_input_vat)
        vat_payable = _round(output_vat - total_input_vat)

        lines = [
            "═══════════════════════════════════════════",
            "        РАСЧЁТ СДЕЛКИ — СИНКОТЕКА",
            "═══════════════════════════════════════════",
            "",
            f"ПРИХОД",
            f"  С НДС:       {income_incl_vat:>16,.2f} руб.",
            f"  Без НДС:     {income_ex_vat:>16,.2f} руб.",
            f"  НДС {output_vat_rate}%:   {output_vat:>16,.2f} руб.",
            "",
            "─── АВТОРСКИЕ ВЫПЛАТЫ ─────────────────────",
        ]

        for name, role, auth_ex, auth_incl, in_vat, note in payee_rows:
            lines.append(f"  {name} ({role})")
            lines.append(f"    Без НДС: {auth_ex:>12,.2f}  С НДС: {auth_incl:>12,.2f}  НДС к зачёту: {in_vat:>10,.2f}")
            lines.append(f"    {note}")

        lines += [
            "",
            f"  ИТОГО авторских без НДС: {total_author_ex:>10,.2f} руб.",
            f"  ИТОГО авторских с НДС:   {total_author_incl:>10,.2f} руб.",
            f"  ИТОГО авт. НДС к зачёту: {total_input_vat:>10,.2f} руб.",
            "",
            "═══════════════════════════════════════════",
            "  ИТОГ",
            "═══════════════════════════════════════════",
            f"  Маржа (без НДС):         {margin:>10,.2f} руб.",
            f"  НДС к уплате:            {vat_payable:>10,.2f} руб.",
            "",
            f"  ВЫВОД: {margin:,.2f} руб. + НДС {output_vat_rate}% к уплате: {vat_payable:,.2f} руб.",
            "═══════════════════════════════════════════",
        ]

        return "\n".join(lines)
