/**
 * Created by FindCanary, LLC.
 * Project: 4over
 */

define([
    'underscore',
    'jquery',
    'FourOver_ProductCustomOptions/js/model/custom-option-price',
    'FourOver_ProductRunSize/js/model/custom-run-size-price',
    'priceUtils'
], function (_, $, customOptionPrice, customRunSizePrice, priceUtils) {
    'use strict';

    var productPriceCalculator = null;

    var getCustomOptionPrice = function (qty, optionsConfig, optionElement) {
        var $optionElement = $(optionElement),
            optionId = priceUtils.findOptionId($optionElement[0]),
            optionConfig = optionsConfig[optionId],
            optionType = $optionElement.prop('type'),
            optionValue = $optionElement.val(),
            price = 0;

        switch (optionType) {
            case 'text':
            case 'textarea':
                if (optionValue) {
                    price += customOptionPrice.calculatePrice(optionConfig.type, optionConfig.prices.oldPrice.amount, qty);
                }
                break;

            case 'radio':
                if ($optionElement.is(':checked') && optionConfig[optionValue] && optionConfig[optionValue].prices) {
                    price += customOptionPrice.calculatePrice(optionConfig[optionValue].type, optionConfig[optionValue].prices.oldPrice.amount, qty);
                }
                break;

            case 'select-one':
                if (optionConfig[optionValue] && optionConfig[optionValue].prices) {
                    price += customOptionPrice.calculatePrice(optionConfig[optionValue].type, optionConfig[optionValue].prices.oldPrice.amount, qty);
                }
                break;

            case 'select-multiple':
                _.each(optionConfig, function (row, optionValueCode) {
                    if (_.contains(optionValue, optionValueCode)) {
                        price += customOptionPrice.calculatePrice(row.type, row.prices.oldPrice.amount, qty);
                    }
                });
                break;

            case 'checkbox':
                if ($optionElement.is(':checked')) {
                    price += customOptionPrice.calculatePrice(optionConfig[optionValue].type, optionConfig[optionValue].prices.oldPrice.amount, qty);
                }
                break;

            case 'file':
                // Checking for 'disable' property equal to checking DOMNode with id*="change-"
                if (optionValue || $optionElement.prop('disabled')) {
                    price += customOptionPrice.calculatePrice(optionConfig.type, optionConfig.prices.oldPrice.amount, qty);
                }
                break;
        }

        return price;
    };

    return {
        setProductPriceCalculator: function (priceCalculator) {
            productPriceCalculator = priceCalculator;
        },

        getUnitPrice: function (qty) {
            return _.isFunction(productPriceCalculator) ? productPriceCalculator(qty) : undefined;
        },

        getItemTotalPrice: function (qty, customQty = 0) {
            var unitPrice = this.getUnitPrice(qty);
            if (customQty > 0) {
                return unitPrice ? unitPrice * customQty : undefined;
            }
            return unitPrice ? unitPrice * qty : undefined;
        },

        getCustomOptionsPrice: function (qty) {
            var customOptionsPrice = 0;
            if (typeof $('#product_addtocart_form').priceOptions === 'function') {
                var optionsConfig = $('#product_addtocart_form').priceOptions('option', 'optionConfig');

                var optionsPrices = _.map(
                    $('.product-custom-option', '#product_addtocart_form'),
                    getCustomOptionPrice.bind(this, qty, optionsConfig)
                );

                customOptionsPrice = _.reduce(optionsPrices, function(memo, num){ return memo + num; }, 0);
            }
            return customOptionsPrice;
        },

        getTotalPrice: function (qty) {
            return this.getItemTotalPrice(qty) + this.getCustomOptionsPrice(qty);
        },

        getCustomTotalPrice: function () {
            const customPrice = customRunSizePrice();
            return customPrice.total_price ?? 0.00;
        }
    };
});
