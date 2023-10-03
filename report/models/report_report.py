import os
import subprocess
import sys
from io import BytesIO, StringIO
from base64 import encodebytes, decodebytes
from zipfile import ZipFile, ZIP_DEFLATED

import docx
import pdfkit
import jinja2
import barcode
from docxtpl import InlineImage, DocxTemplate
from docx.shared import Mm
from xlsx2html import xlsx2html
from xltpl.writerx import BookWriter
from num2words import num2words
from barcode.writer import ImageWriter
from PIL import Image

from odoo import models, fields, api, _
from odoo.tools.mimetypes import guess_mimetype
from odoo.exceptions import UserError
import logging


DEFAULT_PYTHON_CODE = """# Available variables:
#  - env: Odoo Environment on which the action is triggered
#  - model: Odoo Model of the record on which the action is triggered; is a void recordset
#  - record: record on which the action is triggered; may be void
#  - records: recordset of all records on which the action is triggered in multi-mode; may be void
#  - time, datetime, dateutil, timezone: useful Python libraries
#  - float_compare: Odoo function to compare floats based on specific precisions
#  - log: log(message, level='info'): logging function to record debug information in ir.logging table
#  - UserError: Warning Exception to use with raise
#  - Command: x2Many commands namespace
# To return an action, assign: action = {...}\n\n\n\n"""

REPORT_TYPES = [
    ("docx", "docx"),
    ("xlsx", "xlsx"),
    ("pdf", "pdf"),
    ("txt", "txt"),
]

doc = False

_logger = logging.getLogger(__name__)


class ReportReport(models.Model):
    _name = "report.report"
    _description = "Report"

    name = fields.Char(string="Name", required=True)
    model_id = fields.Many2one(comodel_name="ir.model", string="Model")
    report_type = fields.Selection(selection=REPORT_TYPES, string="Report type", required=True)
    template = fields.Binary(string="Template", required=True, attachment=False)
    template_name = fields.Char(string="Template name")
    is_single = fields.Boolean(string="Single")
    code = fields.Text(string="Python Code",
                       default=DEFAULT_PYTHON_CODE,
                       help="Write Python code that the action will execute. Some variables are "
                            "available for use; help about python expression is given in the help tab.")

    @api.constrains("name", "model_id")
    def _check_name_model_id(self):
        """
        Computes actions server for adding in report menu.
        """
        for record_id in self:
            actions_server_id = self.env["ir.actions.server"].search([("report_id", "=", record_id.id)], limit=1)
            if actions_server_id:
                actions_server_id.write({
                    "name": record_id.name,
                    "model_id": record_id.model_id.id,
                    "binding_model_id": record_id.model_id.id,
                })
            else:
                self.env["ir.actions.server"].create({
                    "name": record_id.name,
                    "model_id": record_id.model_id.id,
                    "binding_model_id": record_id.model_id.id,
                    "state": "code",
                    "binding_type": "report",
                    "report_id": record_id.id
                })

    def render_address(self, partner_id):
        """Returns address of partner."""
        address_elements = [
            partner_id.street,
            partner_id.city,
            partner_id.state_id.name,
            partner_id.zip,
            partner_id.country_id.name,
        ]
        return ", ".join([str(x) for x in address_elements if x])

    def replace_barcode(self, value, type, width, height, name, write_text=False):
        """Replaces barcode in template by name."""
        global doc
        value = str(value)
        EAN = barcode.get_barcode_class(type)
        my_ean = EAN(value, writer=ImageWriter(), add_checksum=False)
        fp = BytesIO()
        if write_text:
            options = {
                "module_width": width,
                "module_height": height,
                "write_text": True,
                "quiet_zone": 0,
                "dpi": 300,
                "font_size": 10,
                "text_distance": 0.5,
            }
        else:
            options = {
                "module_width": width,
                "module_height": height,
                "write_text": False,
                "quiet_zone": 0,
                "dpi": 300,
                "text_distance": 0,
                "font_size": 0,
            }
        my_ean.write(fp, options=options)
        fp.seek(0)
        doc.replace_pic(name, fp)
        return ""

    def replace_image(self, value, name):
        """Replaces image in document."""
        global doc
        image = Image.open(BytesIO(decodebytes(value)))
        image_size = max(image.size)
        background_image = Image.new("RGB", (image_size, image_size), "white")
        background_image.paste(image)
        fp = BytesIO()
        background_image.save(fp, "png")
        fp.seek(0)
        doc.replace_pic(name, fp)
        return ""

    def render_barcode(self, value, type, width, height, write_text=False):
        """Generates barcode by data."""
        global doc
        value = str(value)
        EAN = barcode.get_barcode_class(type)
        my_ean = EAN(value, writer=ImageWriter(), add_checksum=False)
        fp = BytesIO()
        if write_text:
            options = {
                "module_width": width,
                "module_height": height,
                "write_text": True,
                "quiet_zone": 0,
                "dpi": 300,
                "font_size": 10,
                "text_distance": 0.5,
            }
        else:
            options = {
                "module_width": width,
                "module_height": height,
                "write_text": False,
                "quiet_zone": 0,
                "dpi": 300,
                "text_distance": 0,
                "font_size": 0,
            }
        my_ean.write(fp, options=options)
        return InlineImage(doc, fp, height=Mm(height))

    def _update_shipment_details_context(self, eval_context):
        """Updates context for shipment details report."""
        record_id = eval_context["record"]
        eval_context["partner_address"] = self.render_address(record_id.odm_company_id or self.env.company.partner_id)
        recipient_data_elements = [
            record_id.partner_id.parent_id.name,
            record_id.partner_id.name,
            record_id.partner_id.parent_id.phone,
        ]
        eval_context["recipient_data"] = ", ".join([str(x) for x in recipient_data_elements if x])
        recipient_address_elements = [
            record_id.partner_id.name,
            record_id.partner_id.street,
            record_id.partner_id.city,
            record_id.partner_id.state_id.name,
            record_id.partner_id.zip,
            record_id.partner_id.country_id.name,
            record_id.partner_id.phone,
        ]
        eval_context["recipient_address"] = ", ".join([str(x) for x in recipient_address_elements if x])
        packages = {}
        for line_id in record_id.move_line_ids_without_package:
            package_id = line_id.result_package_id
            if not packages.get(package_id.name):
                packages[package_id.name] = []
            value = str(line_id.lot_id.name or "")
            EAN = barcode.get_barcode_class("code39")
            my_ean = EAN(value, writer=ImageWriter(), add_checksum=False)
            fp = BytesIO()
            options = {
                "write_text": False,
                "quiet_zone": 0,
                "dpi": 300,
                "text_distance": 0,
                "font_size": 0,
            }
            my_ean.write(fp, options=options)
            packages[package_id.name].append({
                "package_name": package_id.name,
                "package_weight": package_id.shipping_weight,
                "product_name": line_id.product_id.odm_name,
                "lot_name": line_id.lot_id.name,
                "barcode": Image.open(fp),
            })
        eval_context["packages"] = dict(sorted(packages.items()))
        _logger.info("package"*100)
        _logger.info(eval_context["packages"])
        if record_id.odm_company_id.image_1920:
            image = Image.open(BytesIO(decodebytes(record_id.odm_company_id.image_1920)))
            image_w, image_h = image.size
            background_image = Image.new("RGB", (int(image_w * 3.5), image_h), "white")
            background_image.paste(image)
            fp = BytesIO()
            background_image.save(fp, "png")
            eval_context["image"] = Image.open(fp)
        elif self.env.company.partner_id.image_1920:
            image = Image.open(BytesIO(decodebytes(self.env.company.partner_id.image_1920)))
            image_w, image_h = image.size
            background_image = Image.new("RGB", (int(image_w * 3.5), image_h), "white")
            background_image.paste(image)
            fp = BytesIO()
            background_image.save(fp, "png")
            eval_context["image"] = Image.open(fp)
        else:
            eval_context["image"] = False

    def run(self, eval_context):
        """Creates report."""
        if self.is_single:
            eval_context["records"] = [eval_context["records"]]
        jinja_env = jinja2.Environment()
        jinja_env.filters["render_barcode"] = self.render_barcode
        jinja_env.filters["replace_barcode"] = self.replace_barcode
        jinja_env.filters["replace_image"] = self.replace_image
        report_files = self._create_reports(eval_context, jinja_env)
        report_file = self._merge_into_one_file(report_files)
        attachment_id = self._create_attachment(report_file)
        return self.download(attachment_id)

    def download(self, attachment_id) -> dict:
        """Download report."""
        if self.report_type == "pdf":
            return {
                "type": "ir.actions.report",
                "report_type": "qweb-pdf",
                # "close_on_report_download": True,
                "url": f"/web/content/{attachment_id.id}",
                "target": "new",
            }
        return {
            "type": "ir.actions.act_url",
            # "close_on_report_download": True,
            "url": f"/web/content/{attachment_id.id}?download=true",
            "target": "new",
        }

    @staticmethod
    def formatting_float(number: float) -> str:
        """Returns number with comma."""
        if number is False:
            return "0,00"
        return "{:.2f}".format(round(number, 2)).replace(".", ",")

    @staticmethod
    def price2words(price: float) -> str:
        """Returns price in rubles and kopecks as words."""
        if price is False:
            return ""
        rubles = num2words(int(price), lang="ru")
        kopecks = num2words(round((price - int(price)) * 100), lang="ru")
        return f"{rubles} руб. {kopecks} коп."

    def _create_attachment(self, report_file):
        """Creates and returns attachment."""
        return self.env["ir.attachment"].create(
            {
                "name": report_file.name,
                "datas": encodebytes(report_file.getvalue()),
            }
        )

    def _merge_into_one_file(self, reports) -> BytesIO:
        """Returns one final report file docx, xlsx, pdf, txt or zip archive."""
        if len(reports) > 1:
            return self._create_zip_archive(reports)
        return reports[0]

    def _create_reports(self, eval_context, jinja_env) -> list:
        """Return list of reports."""
        reports = []
        for index, record in enumerate(eval_context["records"]):
            eval_context.update({
                "record": record,
                "record_number": index,
            })
            if self.template_name == "ShipmentDetails.xlsx":
                self._update_shipment_details_context(eval_context)
            reports.append(self._create_report(eval_context, jinja_env))
        return reports

    def _create_report(self, eval_context, jinja_env) -> BytesIO:
        """Creating single report file."""
        _logger.info(self.report_type)
        if self.report_type == "docx":
            report = self._create_docx_report(eval_context, jinja_env)
        elif self.report_type == "xlsx":
            report = self._create_xlsx_report(eval_context, jinja_env)
        elif self.report_type == "pdf":
            report = self._create_pdf_report(eval_context, jinja_env)
        else:
            report = self._create_txt_report(eval_context, jinja_env)
        return report

    def _create_docx_report(self, eval_context, jinja_env) -> BytesIO:
        """Creating docx report."""
        writer = DocxTemplate(BytesIO(decodebytes(self.template)))
        global doc
        doc = writer
        writer.render(eval_context, jinja_env)
        docx_report = BytesIO()
        writer.save(docx_report)
        docx_report.name = f"{self.name}.docx"
        return docx_report

    def _create_xlsx_report(self, eval_context, jinja_env) -> BytesIO:
        """Creating xlsx report."""
        _logger.info("_create_xlsx_report"*100)
        _logger.info(eval_context)
        writer = BookWriter(BytesIO(decodebytes(self.template)))
        eval_context["sheet_name"] = self.name
        writer.render_book(payloads=[eval_context])
        xlsx_report = BytesIO()
        writer.save(xlsx_report)
        xlsx_report.name = f"{self.name}.xlsx"
        _logger.info("_create_xlsx_reportend"*100)
        _logger.info(eval_context)
        return xlsx_report

    def _create_txt_report(self, eval_context, jinja_env) -> BytesIO:
        """Creating txt report."""
        docx_report = self._create_docx_report(eval_context, jinja_env)
        txt_report = BytesIO()
        doc = docx.Document(docx_report)
        text = ""
        for paragraph in doc.paragraphs:
            text += f"{paragraph.text}\n"
        txt_report.write(text.encode())
        txt_report.name = f"{self.name}.txt"
        return txt_report

    def _create_pdf_report(self, eval_context, jinja_env) -> BytesIO:
        """Creating pdf report."""
        report, report_type = self._get_original_report_and_type(eval_context, jinja_env)
        if report_type == "docx":
            return self.docx2pdf(report)
        return self.xlsx2pdf(report)

    def _get_original_report_and_type(self, eval_context, jinja_env) -> [BytesIO, str]:
        """Return original report and type."""
        if guess_mimetype(decodebytes(self.template)).split('/')[1] == "vnd.openxmlformats-officedocument.wordprocessingml.document":
            return self._create_docx_report(eval_context, jinja_env), "docx"
        return self._create_xlsx_report(eval_context, jinja_env), "xlsx"

    def docx2pdf(self, docx_report) -> BytesIO:
        """Converting docx to pdf format."""
        attachment_id = self.env["ir.attachment"].create(
            {
                "name": docx_report.name,
                "datas": encodebytes(docx_report.getvalue()),
            }
        )
        file_path = os.path.abspath(attachment_id._full_path(attachment_id.store_fname))
        if sys.platform == "win32":
            outdir = "\\".join(file_path.split("\\")[:-1])
        else:
            outdir = "/".join(file_path.split("/")[:-1])
        new_file_path = f"{outdir}/{docx_report.name}"
        _logger.info('$' * 100)
        os.rename(file_path, new_file_path)
        args = [self._get_libreoffice_exec(), "--convert-to", "pdf:draw_pdf_Export:{\"MaxImageResolution\":{\"type\":\"long\",\"value\":\"1200\"}}", "--outdir", outdir, new_file_path]
        _logger.info('*' * 100)
        _logger.info(args)
        proc = subprocess.run(args, stderr=subprocess.PIPE, stdout=subprocess.PIPE, timeout=60)
        _logger.info('*' * 100)
        _logger.info(proc)
        # if proc.stderr:
        #     raise UserError(f"An error occurred when converting to pdf {proc.stderr}")
        file_path = new_file_path.replace("docx", "pdf")
        with open(file_path, "rb") as f:
            pdf_report = BytesIO(f.read())
        pdf_report.name = docx_report.name.replace("docx", "pdf")
        os.remove(file_path)
        os.remove(new_file_path)
        attachment_id.unlink()
        return pdf_report

    def xlsx2pdf(self, xlsx_report) -> BytesIO:
        """Converting xlsx to pdf format."""
        attachment_id = self.env["ir.attachment"].create(
            {
                "name": xlsx_report.name,
                "datas": encodebytes(xlsx_report.getvalue()),
            }
        )
        file_path = os.path.abspath(attachment_id._full_path(attachment_id.store_fname))
        if sys.platform == "win32":
            outdir = "\\".join(file_path.split("\\")[:-1])
        else:
            outdir = "/".join(file_path.split("/")[:-1])
        new_file_path = f"{outdir}/{xlsx_report.name}"
        os.rename(file_path, new_file_path)
        _logger.info("report"*100)
        _logger.info(file_path, new_file_path)
        _logger.info(sys.platform)
        args = [self._get_libreoffice_exec(), "--convert-to", "pdf:draw_pdf_Export:{\"MaxImageResolution\":{\"type\":\"long\",\"value\":\"1200\"}}", "--outdir", outdir, new_file_path]
        proc = subprocess.run(args, stderr=subprocess.PIPE, stdout=subprocess.PIPE, timeout=10)
        _logger.info(proc)
        # if proc.stderr:
        #     raise UserError(f"An error occurred when converting to pdf {proc.stderr}")
        file_path = new_file_path.replace("xlsx", "pdf")
        with open(file_path, "rb") as f:
            pdf_report = BytesIO(f.read())
        pdf_report.name = xlsx_report.name.replace("xlsx", "pdf")
        os.remove(file_path)
        os.remove(new_file_path)
        attachment_id.unlink()
        return pdf_report

    @staticmethod
    def _get_libreoffice_exec() -> str:
        """Return path to libreoffice executable."""
        if sys.platform == "linux":
            return "libreoffice"
        elif sys.platform == "win32":
            return "C:/Program Files/LibreOffice/program/soffice.exe"
        elif sys.platform == "darwin":
            return "/Applications/LibreOffice.app/Contents/MacOS/soffice"
        raise UserError(_("Unknown operating system for pdf conversion."))

    def _create_zip_archive(self, reports) -> BytesIO:
        """Creating zip archive with multiple reports."""
        zip_archive = BytesIO()
        with ZipFile(zip_archive, "a", ZIP_DEFLATED, False) as zip_file:
            for index, report in enumerate(reports, 1):
                report_name = report.name.replace(".", f" ({index}).")
                zip_file.writestr(report_name, report.getvalue())
        zip_archive.name = self.name + ".zip"
        return zip_archive

    def unlink(self):
        for record_id in self:
            self.env["ir.actions.server"].search([("report_id", "=", record_id.id)]).unlink()
