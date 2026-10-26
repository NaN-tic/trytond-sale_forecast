# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
from trytond.pool import Pool

from . import comparison, forecast, product


def register():
    Pool.register(
        comparison.Comparison,
        comparison.ComparisonProduct,
        comparison.ComparisonCategory,
        comparison.ComparisonLine,
        product.Package,
        product.Template,
        forecast.SalesForecastSaleHistory,
        forecast.SalesForecastSaleHistoryUpdateStart,
        forecast.SalesForecastCalendarCalculateDistributionsStart,
        forecast.SalesForecastCalendarCalculateEventResultsStart,
        forecast.SalesForecastCalendar,
        forecast.SalesForecastCalendarDay,
        forecast.SalesForecastCalendarDayProduct,
        forecast.SalesForecastCalendarDaySpecial,
        forecast.SalesForecastDistribution,
        forecast.SalesForecastEventResult,
        forecast.SalesForecastManualLine,
        forecast.SalesForecastSelectedProduct,
        forecast.SalesForecastCalculationProduct,
        forecast.SalesForecastCalculationCategoryIncrease,
        forecast.SalesForecastCalculationProductIncrease,
        forecast.SalesForecastCalculation,
        forecast.SalesForecast,
        forecast.SalesForecastCategoryIncrease,
        forecast.SalesForecastProductIncrease,
        forecast.SalesForecastLine,
        forecast.SalesForecastDailyLine,
        forecast.SalesForecastMonthlyLine,
        module='sale_forecast', type_='model')
    Pool.register(
        forecast.SalesForecastSaleHistoryUpdate,
        forecast.SalesForecastCalendarCalculateDistributions,
        forecast.SalesForecastCalendarCalculateEventResults,
        module='sale_forecast', type_='wizard')
