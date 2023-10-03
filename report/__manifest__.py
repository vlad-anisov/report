{
    "name": "Report",
    "version": "16.0",
    "category": "",
    "summary": "Summary",
    "description": """ Description """,
    "depends": [
        "base",
    ],
    "author": "",
    "license": "",
    "website": "",
    "data": [
        "data/report_data.xml",
        "security/ir.model.access.csv",
        "views/report_report_view.xml",
    ],
    "assets": {
        'web.assets_backend': [

        ],
    },
    "application": True,
    "external_dependencies": {
        "python": ["docxtpl", "xltpl", "Pillow", "num2words", "Jinja2", "xlsx2html", "python-barcode", "python-docx"],
    },
}

