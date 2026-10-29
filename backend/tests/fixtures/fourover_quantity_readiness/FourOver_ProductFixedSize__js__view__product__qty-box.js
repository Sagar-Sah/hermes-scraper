/**
 * Created by FindCanary, LLC.
 * Project: 4over
 */

define([
    'underscore',
    'ko',
    'uiComponent',
    'FourOver_Project/js/model/selected-runsize',
    'FourOver_Project/js/model/product-price-processor',
    'FourOver_Project/js/model/product-tier-prices',
    'FourOver_ProductStepConfig/js/model/config',
    'priceUtils'
], function (_, ko, Component, selectedRunsize, productPriceProcessor, productTierPrices, productStepConfig, priceUtils) {
    'use strict';

    return Component.extend({
        defaults: {
            template: 'FourOver_ProductFixedSize/product/fixed-size',
            qty: selectedRunsize,
            lineLimitThreshold: 7,
            lineLimit: 7,
            tierPrices: productTierPrices,
            markupInterest: 0,
            isMarkupEnabled: productStepConfig.isMarkupEnabled(),
            isPrintReadyEnabled: false,
            tooltipImageUrl: '',
            imports: {
                resetVars: 'project-tabs:reset-project'
            },
            exports: {
                isVisible: 'project:qtySelectStatus'
            }
        },

        initObservable: function () {
            this._super();
            this.observe([
                'lineLimit',
                'markupInterest'
            ]);

            productTierPrices.subscribe(function (prices) {
                this.lineLimit(this.lineLimitThreshold);
            }, this);

            this.isVisible = ko.pureComputed(function () {
                return productPriceProcessor.getUnitPrice(this.qty()) > 0;
            }, this);

            return this;
        },

        resetVars: function () {
            this.markupInterest(0);
            this.lineLimit(this.lineLimitThreshold);
        },

        getUnitPrice: function (priceItem) {
            return !!priceItem
                ? this.formatPrice(priceItem.price, 4)
                : this.formatPrice(productPriceProcessor.getUnitPrice(this.qty()), 4);
        },

        getTotalPrice: function (priceItem) {
            return !!priceItem
                ? this.formatPrice(priceItem.price * priceItem.qty, 2)
                : this.formatPrice(productPriceProcessor.getItemTotalPrice(this.qty()), 2);
        },

        getMarkupPrice: function (priceItem) {
            var markupPrice = !!priceItem
                ? priceItem.price * priceItem.qty
                : productPriceProcessor.getItemTotalPrice(this.qty());
            return this.formatPrice(markupPrice * this.getMarkupMultiplier(), 2);
        },

        getMarkupMultiplier: function () {
            var multiplier = parseFloat(this.markupInterest());

            if (multiplier <= 0 || _.isNaN(multiplier)) {
                multiplier = 0;
            }

            return (multiplier + 100) / 100;
        },

        formatPrice: function (price, precision) {
            return priceUtils.formatPrice(price, _.extend(this.priceFormat, {requiredPrecision: precision}));
        },

        escapeSelect: function () {
            return false;
        },

        getTierLabel: function (index) {
            var tiers = this.tierPrices(),
                from = tiers[index] ? Number(tiers[index].qty) : 0,
                next = tiers[index + 1] ? Number(tiers[index + 1].qty) : 0;

            if (!from) {
                return '';
            }

            return next > from ? from + '-' + (next - 1) : String(from);
        },

        isCollapsable: function () {
            return this.tierPrices().length > this.lineLimitThreshold;
        },

        isCollapsed: function () {
            return this.tierPrices().length > this.lineLimit();
        },

        isExpanded: function () {
            return this.lineLimit() > this.lineLimitThreshold;
        },

        collapsibleToggle: function () {
            (this.tierPrices().length > this.lineLimit())
                ? this.showMore()
                : this.showLess()
        },

        showMore: function () {
            this.lineLimit(this.tierPrices().length);
        },

        showLess: function () {
            this.lineLimit(this.lineLimitThreshold);
        },

        correctQty: function () {
            if (this.qty().length === 0 || Number(this.qty()) < 1) {
                this.qty(1);
            }
        }
    });
});
