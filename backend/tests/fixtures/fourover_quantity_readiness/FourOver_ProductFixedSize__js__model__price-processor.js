/**
 * Created by FindCanary, LLC.
 * Project: 4over
 */

define([
    'underscore',
    'FourOver_Project/js/model/product-tier-prices',
    'FourOver_Project/js/model/product-unit-price'
], function (_, productTierPrices, productUnitPrice) {
    'use strict';

    return function (qty) {
        var applicableTierPrices = _.filter(productTierPrices(), function(tierPrice) { return qty >= tierPrice.qty; });
        applicableTierPrices = _.sortBy(applicableTierPrices, 'qty');
        var tierPrice = applicableTierPrices.length > 0 ? _.last(applicableTierPrices) : null;

        var unitPrice = tierPrice && tierPrice.price < productUnitPrice() ? tierPrice.price : productUnitPrice();

        return unitPrice > 0 ? unitPrice : undefined;
    };
});
