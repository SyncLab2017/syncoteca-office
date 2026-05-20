import os
from datetime import datetime
from pathlib import Path
from crewai.tools import BaseTool
from jinja2 import Environment, FileSystemLoader, BaseLoader


SYNC_LICENSE_TEMPLATE = """\
ЛИЦЕНЗИОННОЕ СОГЛАШЕНИЕ НА СИНХРОНИЗАЦИЮ № {{ number }}

г. {{ city }}, {{ date }}

ЛИЦЕНЗИАР: {{ licensor_name }}, именуемый в дальнейшем «Правообладатель»
ЛИЦЕНЗИАТ: {{ licensee_name }}, именуемый в дальнейшем «Лицензиат»

1. ПРЕДМЕТ СОГЛАШЕНИЯ
Правообладатель предоставляет Лицензиату неисключительную лицензию на использование
музыкального произведения: «{{ track_title }}» (исполнитель: {{ artist }})
в составе: {{ media_project }}.

2. ТЕРРИТОРИЯ
{{ territory }}

3. СРОК
{{ term }}

4. СПОСОБЫ ИСПОЛЬЗОВАНИЯ
{{ media_types }}

5. ЛИЦЕНЗИОННОЕ ВОЗНАГРАЖДЕНИЕ
{{ fee }} {{ currency }}
Порядок оплаты: {{ payment_terms }}

6. ИСКЛЮЧИТЕЛЬНОСТЬ
{% if exclusive %}Исключительная лицензия.{% else %}Неисключительная лицензия.{% endif %}

7. ПРИМЕНИМОЕ ПРАВО
Настоящее соглашение регулируется законодательством Российской Федерации
(Гражданский кодекс РФ, часть четвёртая).

ПОДПИСИ СТОРОН:

Правообладатель: ______________________ / {{ licensor_name }}
Лицензиат: ______________________ / {{ licensee_name }}
"""

INVOICE_TEMPLATE = """\
СЧЁТ № {{ number }} от {{ date }}

От: {{ seller_name }}
    ИНН/КПП: {{ seller_inn }}
    {{ seller_address }}

Кому: {{ buyer_name }}
     {{ buyer_address }}

┌─────────────────────────────────────────┬────────┬──────────┬──────────────┐
│ Наименование                            │ Кол-во │ Цена     │ Сумма        │
├─────────────────────────────────────────┼────────┼──────────┼──────────────┤
{% for item in items %}│ {{ "%-39s"|format(item.name) }} │ {{ "%6s"|format(item.qty) }} │ {{ "%8s"|format(item.price) }} │ {{ "%12s"|format(item.total) }} │
{% endfor %}├─────────────────────────────────────────┼────────┼──────────┼──────────────┤
│ ИТОГО                                   │        │          │ {{ "%12s"|format(total) }} │
│ НДС {{ vat_rate }}%                                  │        │          │ {{ "%12s"|format(vat_amount) }} │
│ ВСЕГО С НДС                             │        │          │ {{ "%12s"|format(total_with_vat) }} │
└─────────────────────────────────────────┴────────┴──────────┴──────────────┘

Основание: {{ basis }}
"""


class DocumentTool(BaseTool):
    """Generate legal and financial documents from templates."""

    name: str = "document_generator"
    description: str = (
        "Generate documents: sync_license, invoice, royalty_statement, act. "
        "Input: doc_type (str) and params (dict with document data). "
        "Returns document text and saves to data/ directory."
    )

    def _run(self, doc_type: str, params: dict) -> str:
        generators = {
            "sync_license": self._sync_license,
            "invoice": self._invoice,
        }
        gen = generators.get(doc_type)
        if not gen:
            return f"Unknown doc_type: {doc_type}. Available: {list(generators.keys())}"
        return gen(params)

    def _render(self, template_str: str, params: dict) -> str:
        env = Environment(loader=BaseLoader())
        tmpl = env.from_string(template_str)
        return tmpl.render(**params)

    def _sync_license(self, params: dict) -> str:
        defaults = {
            "number": datetime.now().strftime("%Y%m%d-%H%M"),
            "date": datetime.now().strftime("%d.%m.%Y"),
            "city": "Москва",
            "currency": "RUB",
            "exclusive": False,
        }
        params = {**defaults, **params}
        text = self._render(SYNC_LICENSE_TEMPLATE, params)
        path = self._save(text, "contracts", f"sync_license_{params['number']}.txt")
        return f"Лицензионное соглашение сформировано.\nСохранено: {path}\n\n{text}"

    def _invoice(self, params: dict) -> str:
        defaults = {
            "number": datetime.now().strftime("%Y%m%d-%H%M"),
            "date": datetime.now().strftime("%d.%m.%Y"),
            "vat_rate": 20,
            "seller_name": os.getenv("AGENCY_NAME", "Синкотека"),
            "seller_inn": os.getenv("AGENCY_INN", ""),
        }
        params = {**defaults, **params}
        text = self._render(INVOICE_TEMPLATE, params)
        path = self._save(text, "invoices", f"invoice_{params['number']}.txt")
        return f"Счёт сформирован.\nСохранено: {path}\n\n{text}"

    def _save(self, content: str, subdir: str, filename: str) -> str:
        base = Path(__file__).parents[3] / "data" / subdir
        base.mkdir(parents=True, exist_ok=True)
        filepath = base / filename
        filepath.write_text(content, encoding="utf-8")
        return str(filepath)
