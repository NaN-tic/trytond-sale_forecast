import unittest
from datetime import date, timedelta
from decimal import Decimal

from proteus import Model, Wizard
from trytond.modules.company.tests.tools import create_company, get_company
from trytond.tests.test_tryton import drop_db
from trytond.tests.tools import activate_modules


class TestSalesHistoryFromSales(unittest.TestCase):

    def setUp(self):
        drop_db()
        super().setUp()

    def tearDown(self):
        drop_db()
        super().tearDown()

    def test(self):
        activate_modules('sale_forecast')

        History = Model.get('sales.forecast.sale_history')
        Party = Model.get('party.party')
        ProductTemplate = Model.get('product.template')
        ProductUom = Model.get('product.uom')
        Sale = Model.get('sale.sale')
        Forecast = Model.get('sales.forecast')
        StockLocation = Model.get('stock.location')

        create_company()
        company = get_company()
        unit, = ProductUom.find([('name', '=', 'Unit')])
        warehouse, = StockLocation.find([('code', '=', 'WH')])

        party = Party(name='Sales History Customer')
        address = party.addresses.new()
        address.street = 'Main Street'
        address.city = 'Barcelona'
        party.save()

        template = ProductTemplate(
            name='Sales History Product',
            default_uom=unit,
            sale_uom=unit,
            salable=True,
            type='goods',
            cost_price_method='fixed')
        template.sale_forecast_method = 'key_accounts'
        template.sale_forecast_key_account_source = 'orders'
        template.save()

        def create_sale(quantity):
            sale = Sale(
                party=party,
                invoice_party=party,
                shipment_party=party,
                warehouse=warehouse,
                company=company,
                currency=company.currency,
                sale_date=date.today(),
                shipping_date=date.today())
            line = sale.lines.new()
            line.product = template.products[0]
            line.quantity = quantity
            line.unit = unit
            line.unit_price = Decimal('1')
            sale.save()
            return sale

        confirmed_sale = create_sale(12)
        confirmed_sale.click('quote')
        confirmed_sale.click('confirm')
        self.assertIn(confirmed_sale.state,
            {'confirmed', 'processing', 'done'})
        draft_sale = create_sale(5)
        self.assertEqual(draft_sale.state, 'draft')
        quoted_sale = create_sale(7)
        quoted_sale.click('quote')
        cancelled_sale = create_sale(40)
        cancelled_sale.click('cancel')

        wizard = Wizard('sales.forecast.sale_history.update')
        wizard.execute('update')

        history, = History.find([
                ('company', '=', company.id),
                ('product_template', '=', template.id),
                ('date', '=', date.today()),
                ])
        self.assertEqual(history.quantity, 12.0)

        # A confirmation older than the newest cache row must be picked up.
        backdated = create_sale(3)
        backdated.sale_date = date.today() - timedelta(days=30)
        backdated.shipping_date = backdated.sale_date
        backdated.save()
        backdated.click('quote')
        backdated.click('confirm')
        Wizard('sales.forecast.sale_history.update').execute('update')
        old_history, = History.find([
                ('company', '=', company.id),
                ('date', '=', date.today() - timedelta(days=30)),
                ])
        self.assertEqual(old_history.quantity, 3)

        forecast = Forecast(
            name='Draft and Quotation Orders', company=company,
            calculation_method='key_accounts',
            start_date=date.today() - timedelta(days=date.today().weekday()),
            end_date=date.today() + timedelta(days=6-date.today().weekday()))
        forecast.product_templates.append(template)
        forecast.save()
        forecast.click('calculate')
        forecast.reload()
        self.assertEqual(forecast.lines[0].base_quantity, 12)
        self.assertEqual(forecast.lines[0].current_year_quantity, 12)

        # Removing a draft order affects the forecast, never the sales history.
        draft_sale.click('cancel')
        forecast.click('calculate')
        forecast.reload()
        self.assertEqual(forecast.lines[0].base_quantity, 7)
        history, = History.find([
                ('company', '=', company.id),
                ('date', '=', date.today()),
                ])
        self.assertEqual(history.quantity, 12)
