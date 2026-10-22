import unittest

from proteus import Model
from trytond.model.exceptions import DomainValidationError
from trytond.modules.company.tests.tools import create_company
from trytond.tests.test_tryton import drop_db
from trytond.tests.tools import activate_modules


class TestCustomerForecastValidation(unittest.TestCase):

    def setUp(self):
        drop_db()
        super().setUp()

    def tearDown(self):
        drop_db()
        super().tearDown()

    def test(self):
        activate_modules('sales_forecast')
        create_company()
        Template = Model.get('product.template')
        Uom = Model.get('product.uom')
        unit, = Uom.find([('name', '=', 'Unit')])
        template = Template(name='Forecast Validation', default_uom=unit,
            type='goods', sales_forecast_customer_trucks_per_week=0,
            sales_forecast_customer_pallets_per_truck=0)
        template.save()

        # A previous customer forecast source must not constrain other methods.
        template.sales_forecast_key_account_source = 'customer_forecast'
        template.save()
        template.sales_forecast_method = 'key_accounts'
        for source in ['orders', 'mixed']:
            template.sales_forecast_key_account_source = source
            template.save()

        template.sales_forecast_key_account_source = 'customer_forecast'
        template.sales_forecast_customer_pallets_per_truck = 33
        for trucks in [0, -1]:
            template.sales_forecast_customer_trucks_per_week = trucks
            with self.assertRaises(DomainValidationError):
                template.save()
        template.sales_forecast_customer_trucks_per_week = 2
        for pallets in [0, -1]:
            template.sales_forecast_customer_pallets_per_truck = pallets
            with self.assertRaises(DomainValidationError):
                template.save()
        template.sales_forecast_customer_pallets_per_truck = 33
        template.save()
        template.reload()
        self.assertEqual(template.sales_forecast_customer_trucks_per_week, 2)
        self.assertEqual(template.sales_forecast_customer_pallets_per_truck, 33)

        template.sales_forecast_method = 'general'
        template.sales_forecast_customer_trucks_per_week = 0
        template.sales_forecast_customer_pallets_per_truck = 0
        template.save()
        template.reload()
        self.assertEqual(template.sales_forecast_method, 'general')
        self.assertEqual(template.sales_forecast_customer_trucks_per_week, 0)
        self.assertEqual(template.sales_forecast_customer_pallets_per_truck, 0)
