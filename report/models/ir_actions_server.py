from odoo import models, fields


class IrActionsServer(models.Model):
    _inherit = "ir.actions.server"

    report_id = fields.Many2one(comodel_name="report.report", string="Report")

    def run(self):
        """
        OVERRIDE
        Returns report action when report_id isn't empty.
        """
        if self.sudo().report_id:
            return self.sudo().report_id.run(self._get_eval_context(self))
        return super().run()
