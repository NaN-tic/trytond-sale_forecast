import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from proteus import Model, Wizard
from proteus.config import get_config
from trytond.exceptions import UserError
from trytond.modules.company.tests.tools import create_company, get_company
from trytond.tests.test_tryton import drop_db
from trytond.tests.tools import activate_modules
from trytond.transaction import Transaction


class TestForecastRemainingRules(unittest.TestCase):

    def setUp(self):
        drop_db()
        super().setUp()

    def tearDown(self):
        drop_db()
        super().tearDown()

    def test(self):
        activate_modules('sale_forecast')
        create_company()
        company = get_company()
        config = get_config()
        Template = Model.get('product.template')
        Category = Model.get('product.category')
        Party = Model.get('party.party')
        PartyCategory = Model.get('party.category')
        Sale = Model.get('sale.sale')
        Calendar = Model.get('sales.forecast.calendar')
        Calculation = Model.get('sales.forecast.calculation')
        Comparison = Model.get('sales.forecast.comparison')
        Daily = Model.get('sales.forecast.daily_line')
        Event = Model.get('sales.forecast.event_result')
        Distribution = Model.get('sales.forecast.distribution')
        Uom = Model.get('product.uom')
        unit, = Uom.find([('name', '=', 'Unit')])
        kg, = Uom.find([('name', '=', 'Kilogram')])
        warehouse, = Model.get('stock.location').find([('code', '=', 'WH')])
        family = Category(name='Bread')
        family.save()
        customer_category = PartyCategory(name='Special Customers')
        customer_category.save()
        party = Party(name='Customer')
        party.addresses.new(city='Barcelona')
        party.categories.append(customer_category)
        party.save()

        def product(name, method='general', uom=unit):
            template = Template(name=name, default_uom=uom, sale_uom=uom,
                salable=True, type='goods', cost_price_method='fixed',
                sale_forecast_method=method)
            template.categories.append(Category(family.id))
            template.packages.new(name='Box', quantity=10, is_default=True)
            template.sale_forecast_boxes_per_pallet = 20
            if method == 'new_product':
                template.sale_forecast_new_product_source = 'last_month'
                template.sale_forecast_new_product_increase_percentage = 0
            template.save()
            return template

        def sell(template, when, quantity, draft=False):
            sale = Sale(party=party, invoice_party=party,
                shipment_party=party, company=company, warehouse=warehouse,
                currency=company.currency, sale_date=when, shipping_date=when)
            line = sale.lines.new(product=template.products[0],
                quantity=quantity, unit_price=Decimal('1'))
            line.unit = template.default_uom
            sale.save()
            if not draft:
                sale.click('quote')
                sale.click('confirm')
            return sale

        mixed = product('Mixed Bread', 'key_accounts')
        mixed.sale_forecast_key_account_source = 'mixed'
        mixed.save()
        customer = product('Customer Bread', 'key_accounts')
        customer.sale_forecast_key_account_source = 'customer_forecast'
        customer.sale_forecast_customer_trucks_per_week = 2
        customer.sale_forecast_customer_pallets_per_truck = 3
        customer.save()
        new = product('New Bread', 'new_product')
        empty = product('No Closed History', 'new_product')
        special = product('Special Bread')
        weight = product('Legacy Weight', uom=kg)
        leap = product('Leap Bread')
        for template in (mixed, customer):
            sell(template, date(2023, 3, 6), 80)
            sell(template, date(2023, 3, 12), 20)
        sell(mixed, date(2023, 3, 13), 100)
        order = sell(mixed, date(2024, 3, 4), 700, draft=True)
        # Current March sales must not replace the last closed month (February).
        sell(new, date(2024, 2, 5), 70)
        sell(new, date(2024, 3, 4), 900)
        sell(empty, date(2024, 3, 4), 900)
        # For 2024-03-04, ISO week 10 maps to 2023-03-06, not 2023-03-04.
        sell(special, date(2023, 3, 6), 100)
        sell(special, date(2023, 2, 27), 1000)
        legacy = sell(weight, date(2023, 3, 6), 15)
        legacy_id = legacy.lines[0].id
        with Transaction().start(config.database_name, 0) as transaction:
            table = config.pool.get('sale.line').__table__()
            transaction.connection.cursor().execute(*table.update(
                [table.unit], [unit.id], where=table.id == legacy_id))
            transaction.commit()
        # The leap-year rolling window includes 2023-03-02 and excludes 03-01.
        sell(leap, date(2023, 3, 1), 900)
        sell(leap, date(2023, 3, 2), 100)
        calendar = Calendar(name='Rules Calendar')
        calendar.day_lines.new(date=date(2024, 3, 4), day_type='festive')
        diada = calendar.day_lines.new(date=date(2024, 3, 5), day_type='product_day')
        diada.product_lines.new(product_template=mixed, units=900)
        diada.product_lines.new(product_template=customer, units=5000)
        calendar.save()
        calculation = Calculation(1)
        calculation.company = company
        calculation.calendar = calendar
        calculation.start_date = date(2024, 3, 4)
        calculation.end_date = date(2024, 3, 17)
        calculation.product_templates.append(Template(mixed.id))
        calculation.save()
        self.assertEqual(len(calculation.category_increase_lines), 1)
        calculation.category_increase_lines[0].percentage = 0.2
        calculation.save()
        with patch.object(config.pool.get('ir.date'), 'today',
                return_value=date(2024, 3, 20)):
            calculation.click('calculate_key_accounts')
        calculation.reload()
        forecast = calculation.generated_forecasts[0]
        self.assertEqual([line.units for line in forecast.lines], [700, 120])
        self.assertAlmostEqual(sum(line.units for line in Daily.find([
            ('forecast', '=', forecast.id), ('date', '<=', date(2024, 3, 10))])), 700)
        # Recalculate only week one; the product must still display week two.
        order.lines[0].quantity = 800
        order.lines[0].unit_price = Decimal('1')
        order.save()
        calculation.end_date = date(2024, 3, 10)
        calculation.save()
        calculation.click('calculate_key_accounts')
        mixed.reload()
        self.assertEqual([line.units for line in mixed.sale_forecast_lines], [800, 120])
        report = Comparison(name='Latest by week', company=company,
            start_date=date(2024, 3, 4), end_date=date(2024, 3, 17), unit='units')
        report.products.append(Template(mixed.id))
        report.save()
        report.click('calculate')
        self.assertEqual([r.forecast_quantity for r in report.lines
            if r.kind == 'product'], [800, 120])
        calculation.product_templates.pop()
        calculation.product_templates.append(Template(customer.id))
        calculation.save()
        calculation.click('calculate_key_accounts')
        calculation.reload()
        self.assertEqual(calculation.generated_forecasts[0].lines[0].units, 1200)
        # April forecast made during March uses February, not open March.
        calculation.product_templates.pop()
        calculation.product_templates.append(Template(new.id))
        calculation.start_date = date(2024, 4, 8)
        calculation.end_date = date(2024, 4, 14)
        calculation.save()
        with patch.object(config.pool.get('ir.date'), 'today',
                return_value=date(2024, 3, 20)):
            calculation.click('calculate_new_product')
        calculation.reload()
        self.assertEqual(calculation.generated_forecasts[0].lines[0].base_quantity, 70)
        sell(new, date(2024, 1, 8), 30)
        new.sale_forecast_new_product_source = 'average_months'
        new.save()
        calculation.refresh_parameters = True
        calculation.save()
        with patch.object(config.pool.get('ir.date'), 'today',
                return_value=date(2024, 3, 20)):
            calculation.click('calculate_new_product')
        calculation.reload()
        self.assertEqual(calculation.generated_forecasts[0].lines[0].base_quantity, 50)
        calculation.product_templates.pop()
        calculation.product_templates.append(Template(empty.id))
        calculation.save()
        with patch.object(config.pool.get('ir.date'), 'today',
                return_value=date(2024, 3, 20)), self.assertRaises(UserError):
            calculation.click('calculate_new_product')
        special_calendar = Calendar(name='Special Calendar')
        day = special_calendar.day_lines.new(date=date(2024, 3, 4),
            day_type='special_sale')
        day.special_lines.new(scope='party_category',
            party_category=customer_category, increase_percentage=0.5)
        special_calendar.save()
        with patch('trytond.modules.sale_forecast.forecast.notify') as notice:
            special_calendar.click('calculate_event_results')
        self.assertTrue(notice.called)
        self.assertIn(legacy.number, notice.call_args.kwargs['body'])
        result, = Event.find([('calendar_day.calendar', '=', special_calendar.id),
            ('product_template', '=', special.id)])
        self.assertEqual(result.units, 30)
        self.assertFalse(Event.find([('calendar_day.calendar', '=', special_calendar.id),
            ('product_template', '=', weight.id)]))
        # Exclusions use products sold on the event date, even with no prior-year history.
        exclusion = Calendar(name='Historical Specials')
        day = exclusion.day_lines.new(date=date(2024, 2, 5), day_type='special_sale')
        day.special_lines.new(scope='party_category',
            party_category=customer_category, increase_percentage=0)
        exclusion.save()
        sell(new, date(2024, 2, 6), 30)
        with patch.object(config.pool.get('ir.date'), 'today',
                return_value=date(2024, 3, 1)):
            Wizard('sales.forecast.sale_history.update').execute('update')
            exclusion.click('calculate_distributions')
        monday, = Distribution.find([('product_template', '=', new.id), ('weekday', '=', '0')])
        tuesday, = Distribution.find([('product_template', '=', new.id), ('weekday', '=', '1')])
        # Only January's normal Monday (30) and February's Tuesday (30) remain.
        self.assertEqual(monday.percentage, 50)
        self.assertEqual(tuesday.percentage, 50)
        thursday, = Distribution.find([('product_template', '=', leap.id), ('weekday', '=', '3')])
        self.assertEqual(thursday.percentage, 100)
