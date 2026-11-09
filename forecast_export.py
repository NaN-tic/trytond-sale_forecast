from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from trytond.pool import Pool
from trytond.report import Report
from trytond.transaction import check_access


class LatestForecastsExcel(Report):
    __name__ = 'sales.forecast.latest_excel'

    @classmethod
    def _execute(cls, records, header, data, action):
        pool = Pool()
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = action.name[:31]
        forecast_fields = pool.get('sales.forecast').fields_get()
        line_fields = pool.get('sales.forecast.line').fields_get()
        product_fields = pool.get('product.product').fields_get(['code'])
        sheet.append([
            forecast_fields['company']['string'],
            line_fields['forecast']['string'],
            forecast_fields['computed_at']['string'],
            forecast_fields['calculation_method']['string'],
            product_fields['code']['string'],
            line_fields['product_template']['string'],
            line_fields['product']['string'],
            line_fields['week_start']['string'],
            line_fields['week_end']['string'],
            pool.get('product.template').fields_get(
                ['default_uom'])['default_uom']['string'],
            line_fields['units']['string'],
            line_fields['boxes']['string'],
            line_fields['pallets']['string'],
            ])
        with check_access():
            forecasts = (pool.get('sales.forecast.calculation').latest_forecasts()
                if records else [])
            method_labels = dict(pool.get('sales.forecast').fields_get(
                ['calculation_method'])['calculation_method']['selection'])
            for forecast in forecasts:
                lines = pool.get('sales.forecast.line').search([
                    ('forecast', '=', forecast.id)],
                    order=[('week_start', 'ASC'), ('product_template', 'ASC')])
                for line in lines:
                    sheet.append([forecast.company.rec_name, forecast.name,
                        forecast.computed_at,
                        method_labels.get(forecast.calculation_method),
                        line.product.code if line.product else '',
                        line.product_template.name,
                        line.product.rec_name if line.product else '',
                        line.week_start, line.week_end,
                        line.product_template.default_uom.rec_name,
                        line.units, line.boxes, line.pallets])
        for row in sheet:
            for cell in row:
                # Names and codes are data, never Excel formulas.
                if isinstance(cell.value, str):
                    cell.data_type = 's'
                if cell.row == 1:
                    cell.font = Font(bold=True, color='FFFFFF')
                    cell.fill = PatternFill('solid', fgColor='246B75')
                elif cell.column == 3:
                    cell.number_format = 'dd/mm/yyyy hh:mm:ss'
                elif cell.column in (8, 9):
                    cell.number_format = 'dd/mm/yyyy'
                elif cell.column >= 11:
                    cell.number_format = '#,##0.00'
        widths = [30, 42, 22, 22, 18, 42, 42, 16, 16, 14, 18, 18, 18]
        for index, width in enumerate(widths, 1):
            sheet.column_dimensions[get_column_letter(index)].width = width
        sheet.freeze_panes = 'A2'
        sheet.auto_filter.ref = sheet.dimensions
        output = BytesIO()
        workbook.save(output)
        return 'xlsx', output.getvalue()
