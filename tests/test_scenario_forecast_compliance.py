import unittest
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from proteus import Model, Wizard
from proteus.config import get_config
from trytond.exceptions import UserError
from trytond.model.exceptions import AccessError
from trytond.modules.company.tests.tools import create_company, get_company
from trytond.modules.sales_forecast.forecast import previous_year_date
from trytond.tests.test_tryton import drop_db
from trytond.tests.tools import activate_modules
from trytond.transaction import Transaction


class TestForecastCompliance(unittest.TestCase):

    def setUp(self):
        drop_db()
        super().setUp()

    def tearDown(self):
        drop_db()
        super().tearDown()

    def test(self):
        activate_modules('sales_forecast')
        create_company()
        company = get_company()
        config = get_config()
        Template = Model.get('product.template')
        Uom = Model.get('product.uom')
        Manual = Model.get('sales.forecast.manual_line')
        Calendar = Model.get('sales.forecast.calendar')
        Day = Model.get('sales.forecast.calendar.day')
        ProductDay = Model.get('sales.forecast.calendar.day.product')
        Calculation = Model.get('sales.forecast.calculation')
        Forecast = Model.get('sales.forecast')
        ForecastLine = Model.get('sales.forecast.line')
        Daily = Model.get('sales.forecast.daily_line')
        Monthly = Model.get('sales.forecast.monthly_line')
        Comparison = Model.get('sales.forecast.comparison')
        History = Model.get('sales.forecast.sale_history')
        Distribution = Model.get('sales.forecast.distribution')
        Category = Model.get('product.category')
        Party = Model.get('party.party')
        Sale = Model.get('sale.sale')
        unit, = Uom.find([('name', '=', 'Unit')])
        kg, = Uom.find([('name', '=', 'Kilogram')])
        warehouse, = Model.get('stock.location').find([('code', '=', 'WH')])
        week = date.today() - timedelta(days=date.today().weekday())
        root = Category(name='Families')
        root.save()
        family = Category(name='Bread', parent=root)
        family.save()
        other = Category(name='Other Classification')
        other.save()

        def product(name, method='manual', packaging=True, uom=unit):
            template = Template(name=name, default_uom=uom, sale_uom=uom,
                salable=True, type='goods', cost_price_method='fixed')
            template.sales_forecast_method = method
            template.categories.extend([Category(family.id), Category(other.id)])
            if packaging:
                package = template.packages.new()
                package.name = 'Box'
                package.quantity = 10
                package.is_default = True
                template.sales_forecast_boxes_per_pallet = 20
            template.save()
            return template

        manual = product('Manual Bread')
        general = product('General Bread', 'general')
        unpacked = product('Loose Bread', packaging=False)
        weight = product('Weight Product', 'general', False, kg)
        service_family = Category(name='Services', parent=root)
        service_family.save()
        service = Template(name='Delivery Service', default_uom=unit,
            sale_uom=unit, salable=True, type='service',
            sales_forecast_method='general')
        service.categories.append(service_family)
        service.save()
        party = Party(name='Forecast Customer')
        party.addresses.new(city='Barcelona')
        party.save()

        def sell(template, when, quantity):
            sale = Sale(party=party, invoice_party=party,
                shipment_party=party, warehouse=warehouse, company=company,
                currency=company.currency, sale_date=when, shipping_date=when)
            line = sale.lines.new()
            line.product = template.products[0]
            line.quantity = quantity
            line.unit = template.default_uom
            line.unit_price = Decimal('1')
            sale.save()
            sale.click('quote')
            sale.click('confirm')
            return sale

        past = week - timedelta(days=14)
        for template in (manual, general):
            sell(template, past, 80)
            sell(template, past + timedelta(days=6), 20)
        sell(manual, week, 30)
        sell(service, past, 1000)
        legacy = sell(weight, past, 15)
        legacy_line_id = legacy.lines[0].id
        # Reproduce migrated historical data with an obsolete unit category.
        with Transaction().start(config.database_name, 0) as transaction:
            Line = config.pool.get('sale.line')
            table = Line.__table__()
            cursor = transaction.connection.cursor()
            cursor.execute(*table.update([table.unit], [unit.id],
                    where=table.id == legacy_line_id))
            transaction.commit()
        with patch('trytond.modules.sales_forecast.forecast.notify') as notice:
            Wizard('sales.forecast.sale_history.update').execute('update')
        self.assertTrue(notice.called)
        self.assertIn(legacy.number, notice.call_args.kwargs['body'])
        self.assertFalse(History.find([('product_template', '=', weight.id)]))
        self.assertFalse(History.find([('product_template', '=', service.id)]))

        calendar = Calendar(name='Compliance Calendar')
        historical_day = calendar.day_lines.new()
        historical_day.date = past
        historical_day.day_type = 'product_day'
        line = historical_day.product_lines.new()
        line.product_template = general
        line.units = 80
        festive = calendar.day_lines.new()
        festive.date = week
        festive.day_type = 'festive'
        calendar.save()
        calendar.click('calculate_distributions')
        self.assertFalse(Distribution.find([
            ('product_template', '=', service.id)]))
        general_monday, = Distribution.find([
            ('product_template', '=', general.id), ('weekday', '=', '0')])
        manual_monday, = Distribution.find([
            ('product_template', '=', manual.id), ('weekday', '=', '0')])
        self.assertEqual(general_monday.percentage, 0)
        self.assertGreater(manual_monday.percentage, 0)

        invalid = Day(calendar=calendar, date=week+timedelta(days=2),
            day_type='product_day')
        invalid.product_lines.new(product_template=general)
        with self.assertRaises(UserError):
            invalid.save()
        invalid.product_lines[0].units = 0
        invalid.save()
        self.assertEqual(invalid.product_lines[0].units, 0)

        Manual(product_template=manual, week_start=week, units=700).save()
        manual.click('duplicate_last_manual_forecast')
        no_package = Manual(product_template=unpacked, week_start=week, units=5)
        no_package.save()
        self.assertIsNone(no_package.boxes)
        self.assertIsNone(no_package.pallets)
        with self.assertRaises(UserError):
            Manual(product_template=unpacked,
                week_start=week+timedelta(days=7), boxes=1).save()
        calculation = Calculation(1)
        calculation.company = company
        calculation.calendar = calendar
        calculation.family_category = root
        calculation.start_date = week
        calculation.end_date = week+timedelta(days=13)
        calculation.product_templates.append(Template(manual.id))
        calculation.save()
        calculation.click('calculate_manual')
        calculation.reload()
        self.assertTrue(calculation.generated_forecasts)
        forecast = calculation.generated_forecasts[0]
        self.assertEqual([line.units for line in forecast.lines], [700, 700])
        self.assertEqual(forecast.lines[0].week_label,
            f'{week.isocalendar().week:02d}/{week.isocalendar().year % 100:02d}')
        daily = Daily.find([('forecast', '=', forecast.id)])
        monthly = Monthly.find([('forecast', '=', forecast.id)])
        self.assertAlmostEqual(sum(line.units for line in daily), 1400)
        self.assertAlmostEqual(sum(line.units for line in monthly), 1400)
        forecast.click('open_daily_lines')
        forecast.click('open_month_lines')
        self.assertEqual([line.id for line in daily],
            [line.id for line in Daily.find([('forecast', '=', forecast.id)])])
        self.assertEqual([line.id for line in monthly],
            [line.id for line in Monthly.find([('forecast', '=', forecast.id)])])
        with self.assertRaises(AccessError):
            ForecastLine._proxy.write([forecast.lines[0].id],
                {'quantity': 999}, config.context)
        with self.assertRaises(UserError):
            Forecast._proxy.write([forecast.id], {'name': 'Changed'},
                config.context)
        manual_monday, = Distribution.find([
            ('product_template', '=', manual.id), ('weekday', '=', '0')])
        stamp = manual_monday.computed_at
        calculation.click('calculate_manual')
        self.assertEqual(Distribution(manual_monday.id).computed_at, stamp)
        # A shared calendar timestamp does not imply company results exist.
        with Transaction().start(config.database_name, 0) as transaction:
            Event = config.pool.get('sales.forecast.event_result')
            Event.delete(Event.search([('company', '=', company.id)]))
            transaction.commit()
        calculation.click('calculate_manual')
        self.assertTrue(Model.get('sales.forecast.event_result').find([
            ('company', '=', company.id),
            ('calendar_day.calendar', '=', calendar.id)]))

        report = Comparison(name='Weekly comparison', company=company,
            start_date=week, end_date=week+timedelta(days=20), weekly=True,
            unit='units')
        report.products.append(Template(manual.id))
        report.save()
        report.click('calculate')
        rows = sorted((r for r in report.lines if r.kind == 'product'),
            key=lambda r: r.week_start)
        self.assertEqual([r.forecast_display for r in rows],
            ['700.00', '700.00', 'N/A'])
        self.assertEqual(rows[0].actual_quantity, 30)
        self.assertEqual(rows[0].difference, '670.00')
        total, = [r for r in report.lines if r.kind == 'total']
        self.assertEqual(total.forecast_display, 'N/A')
        report.weekly = False
        report.save()
        self.assertFalse(report.lines)
        report.click('calculate')
        row, = [r for r in report.lines if r.kind == 'product']
        self.assertEqual(row.forecast_display, 'N/A')
        report.end_date = week+timedelta(days=13)
        report.unit = 'boxes'
        report.save()
        report.click('calculate')
        row, = [r for r in report.lines if r.kind == 'product']
        self.assertEqual(row.forecast_display, '140.00')
        self.assertEqual(row.actual_display, '3.00')
        report.method = 'general'
        report.save()
        with self.assertRaises(UserError):
            report.click('calculate')

        calculation.product_templates.pop()
        calculation.product_templates.append(Template(general.id))
        calculation.general_increase_percentage = 0
        calculation.save()
        self.assertEqual([r.category.id for r in calculation.category_increase_lines],
            [family.id])
        calculation.family_category = None
        calculation.save()
        for increase in calculation.category_increase_lines:
            increase.percentage = 0.1 if increase.category.id == family.id else 0.2
        calculation.save()
        with self.assertRaises(UserError):
            calculation.click('calculate_general')
        calculation.product_increase_lines.new(
            product_template=general, percentage=0.3)
        calculation.save()
        calculation.click('calculate_general')
        for increase in calculation.category_increase_lines:
            increase.percentage = 0.1
        calculation.product_templates.pop()
        calculation.save()
        # Remove stale service-only family settings when reloading categories.
        calculation.category_increase_lines.new(
            category=Category(service_family.id), percentage=0.5)
        calculation.save()
        calculation.click('load_general_categories')
        self.assertNotIn(service_family.id,
            [r.category.id for r in calculation.category_increase_lines])
        calculation.click('calculate_general')
        calculation.reload()
        self.assertNotIn(service.id, [line.product_template.id
            for line in calculation.generated_forecasts[0].lines])
        report.method = ''
        report.products.pop()
        report.unit = 'units'
        report.save()
        report.click('calculate')
        self.assertNotIn(service.id,
            [line.product.id for line in report.lines if line.product])
        self.assertEqual(previous_year_date(date(2026, 12, 28)), date(2025, 12, 22))
        self.assertEqual(previous_year_date(date(2025, 1, 6)), date(2024, 1, 8))
        with Transaction().start(config.database_name, 0) as transaction:
            CalendarDay = config.pool.get('sales.forecast.calendar.day')
            CalendarDay.write([CalendarDay(invalid.id)], {'note': '[AUTO] Diada'})
            transaction.commit()
        product_day, = ProductDay.find([('calendar_day', '=', invalid.id)])
        product_day.units = 50
        product_day.save()
        invalid.reload()
        self.assertFalse(invalid.note.startswith('[AUTO]'))
