/**
 * Created by FindCanary, LLC.
 * Project: 4over
 */

define([
    'jquery',
    'underscore',
    'ko',
    'uiComponent',
    'FourOver_Project/js/model/product-package-prices',
    'FourOver_Project/js/model/selected-runsize',
    'FourOver_ProductRunSize/js/model/selected-custom-run-size',
    'FourOver_ProductRunSize/js/model/selected-lower-run-size',
    'FourOver_ProductRunSize/js/model/custom-run-size-price',
    'FourOver_ProductStepConfig/js/model/config',
    'FourOver_Project/js/model/project-tabs/product-options-validator',
    'priceUtils'
], function ($, _, ko, Component, productPackagePrices, selectedQty, selectedCustomRunSize, selectedLowerRunSize, customRunSizePrice, productStepConfig, productOptionsValidator, priceUtils) {
    'use strict';

    return Component.extend({
        defaults: {
            template: 'FourOver_ProductRunSize/product-run-size',
            priceFormat: {},
            panelTitle: 'Run Size',
            hasCustomRunSize: '',
            isRunSizeSearchEnabled: false,
            lineLimitThreshold: 7,
            lineLimit: 7,
            searchQuery: '',
            customRunSizeSearchErrorMessage: false,
            packagePrices: productPackagePrices,
            selectedQty: selectedQty,
            selectedCustomRunSize: selectedCustomRunSize,
            selectedLowerRunSize: selectedLowerRunSize,
            customRunSizePrice: customRunSizePrice,
            markupInterest: 0,
            isMarkupEnabled: productStepConfig.isMarkupEnabled(),
            imports: {
                resetVars: 'project-tabs:reset-project'
            },
            exports: {
                isVisible: 'project:qtySelectStatus'
            }
        },

        initialize: function (config) {
            this._super();

            if (config && typeof config.isPrintReadyEnabled !== 'undefined') {
                this.isPrintReadyEnabled = config.isPrintReadyEnabled;
            }

            productOptionsValidator.registerValidator('customRunSize', this.isCustomRunSizeValid.bind(this));

            return this;
        },

        isCustomRunSizeValid: function () {
            if (!this.isCustomRunSizeEnable()) {
                return true;
            }

            let entered = Number(this.selectedCustomRunSize()),
                min = this.minRunSize(),
                max = this.maxRunSize();

            if (!entered || isNaN(entered)) {
                return true;
            }

            if (entered >= min && entered <= max) {
                return true;
            }

            this.customRunSizeErrorMessage(`Value must be between ${min} and ${max}`);

            return false;
        },

        initObservable: function () {
            this._super();
            this.observe([
                'lineLimit',
                'markupInterest',
                'searchQuery',
                'customRunSizeSearchErrorMessage'
            ]);

            this.customRunSizeErrorMessage = ko.observable('');
            this.customRunSizeUnitPrice = ko.observable(this.formatPrice(0.00, 2));
            this.customRunSizeTotalPrice = ko.observable(this.formatPrice(0.00, 2));
            this.customRunSizeUnitPrices = ko.observableArray([]);

            this.minRunSize = ko.observable(0);
            this.maxRunSize = ko.observable(0);

            productPackagePrices.subscribe(function (prices) {
                this.customRunSizeUnitPrices([]);

                prices.forEach(price => {
                    let unitPrice = price.price / price.qty;
                    this.customRunSizeUnitPrices.push({ qty: price.qty, unitPrice: unitPrice, price: price.price });
                });

                this.minRunSize(Math.min(...this.customRunSizeUnitPrices().map(item => item.qty)));
                this.maxRunSize(Math.max(...this.customRunSizeUnitPrices().map(item => item.qty)));

                if (this.selectedCustomRunSize()) {
                    let qty = this.selectedCustomRunSize();
                    this.customRunSizePriceUiUpdate(qty);
                }

                if (_.isUndefined(_.findWhere(prices, {qty: this.selectedQty()}))) {
                    this.selectedQty(_.first(prices).qty);
                }

                var minQtyPrice = _.min(prices, function (price) { return price.qty; });
                var maxQtyPrice = _.max(prices, function (price) { return price.qty; });

                if (this.selectedQty() < minQtyPrice.qty) {
                    this.selectedQty(minQtyPrice.qty);
                } else if (this.selectedQty() > maxQtyPrice.qty) {
                    this.selectedQty(maxQtyPrice.qty);
                }

                this.lineLimit(this.lineLimitThreshold);
            }, this);

            this.isVisible = ko.pureComputed(function () {
                return this.packagePrices().length > 0;
            }, this);

            this.filteredPackagePrices = ko.pureComputed(function () {
                let query = this.searchQuery().toString().trim();
                if (!query) {
                    return this.packagePrices();
                }
                return this.packagePrices().filter(priceItem =>
                    priceItem.qty.toString().startsWith(query)
                );
            }, this);

            this.runSizeInput = ko.computed({
                read: function () {
                    var entered = this.selectedCustomRunSize();

                    return entered > 0 ? String(entered) : '';
                },
                write: function (value) {
                    this.applyCustomRunSize(Number(value));
                },
                owner: this
            });

            this.filteredPackagePrices.subscribe(function(newValue) {
                if (this.searchQuery().toString().trim() && this.filteredPackagePrices().length === 0) {
                    this.customRunSizeSearchErrorMessage(true);
                } else {
                    this.customRunSizeSearchErrorMessage(false);
                }
            }, this);

            return this;
        },

        resetVars: function () {
            this.markupInterest(0);
            this.lineLimit(this.lineLimitThreshold);
        },

        isCustomRunSizeEnable: function () {
            return this.hasCustomRunSize;
        },

        hasRunSizeSearchEnabled: function() {
            return this.isRunSizeSearchEnabled;
        },

        isCustomRunSizeSelected: function () {
            return this.selectedQty() == 'custom' || this.selectedCustomRunSize() > 0;
        },

        customRunSizePriceUiUpdate: function (enteredQty) {
            if (isNaN(enteredQty) || enteredQty < this.minRunSize() || enteredQty > this.maxRunSize()) {
                if (enteredQty > 0) {
                    this.customRunSizeErrorMessage(`Value must be between ${this.minRunSize()} and ${this.maxRunSize()}`);
                } else {
                    this.customRunSizeErrorMessage('');
                }

                this.customRunSizeUnitPrice(this.formatPrice(0.00, 2));
                this.customRunSizeTotalPrice(this.formatPrice(0.00, 2));
                this.selectedLowerRunSize('');
                this.setCustomRunSizePrices('');
                this.selectedQty('custom');
                return;
            }

            this.customRunSizeErrorMessage('');

            let closestLower = this.customRunSizeUnitPrices().reduce((prev, curr) => {
                return curr.qty <= enteredQty ? curr : prev;
            });

            let closestUpper = this.customRunSizeUnitPrices().reduce((prev, curr) => {
                if (curr.qty >= enteredQty) {
                    return !prev || curr.qty < prev.qty ? curr : prev;
                }
                return prev;
            }, null);

            this.calculateCustomRunSizePrice(closestLower.price, closestUpper.price, closestLower.qty, closestUpper.qty, enteredQty);

        },

        calculateCustomRunSizePrice: function (lowerPrice, upperPrice, lowerQuantity, upperQuantity, requestedQuantity) {
            let customRunTotalPrice = 0;
            let customRunSizeUnitPrice = 0;

            if (lowerQuantity != requestedQuantity) {
                customRunTotalPrice = lowerPrice + ((requestedQuantity - lowerQuantity) / (upperQuantity - lowerQuantity)) * (upperPrice - lowerPrice);

                customRunSizeUnitPrice = customRunTotalPrice / requestedQuantity;
            } else {
                customRunTotalPrice = lowerPrice;

                customRunSizeUnitPrice = customRunTotalPrice / requestedQuantity;
            }

            this.customRunSizePrice({
                'total_price': customRunTotalPrice,
                'unit_price': customRunSizeUnitPrice
            });
            this.setCustomRunSizePrices(customRunSizeUnitPrice, customRunTotalPrice, lowerQuantity, upperQuantity);
        },

        applyCustomRunSize: function (enteredQty) {
            var qty = isNaN(enteredQty) ? 0 : Number(enteredQty);

            this.selectedCustomRunSize(qty);
            this.customRunSizeErrorMessage('');
            this.setCustomRunSizePrices('');
            this.selectedQty('custom');
            this.customRunSizePriceUiUpdate(qty);
        },

        runSizeRangeHint: function () {
            var min = this.minRunSize(),
                max = this.maxRunSize();

            if (!isFinite(min) || !isFinite(max) || !min || !max || min === max) {
                return '';
            }

            return min + '-' + max;
        },

        customRunSizePriceUpdate: function (data, event) {
            let enteredQty = !isNaN(event.target.value) ? Number(event.target.value) : 0;
            this.selectedCustomRunSize(enteredQty);
            this.customRunSizeErrorMessage('');
            this.setCustomRunSizePrices('');
            this.selectedQty('custom');

            if (event.key >= '0' && event.key <= '9') {
                clearTimeout(this.typingTimeout);
                this.typingTimeout = setTimeout(() => {
                    this.customRunSizePriceUiUpdate(enteredQty);
                }, 800);
            } else {
                this.customRunSizePriceUiUpdate(enteredQty);
            }
        },

        setCustomRunSizePrices: function (unitPrice, totalPrice, closestLowerQty, closestUpperQty) {
            this.customRunSizeUnitPrice(this.formatPrice(unitPrice, 4));
            this.customRunSizeTotalPrice(this.formatPrice(totalPrice, 2));
            this.selectedLowerRunSize(closestLowerQty);
            this.selectedQty(closestUpperQty);
        },

        customRunSizeMarkUpPrice: function () {
            let customRunSizeTotalPriceWithOutSymbol = this.customRunSizeTotalPrice().replace(/[^0-9.-]+/g, '');
            return this.formatPrice(customRunSizeTotalPriceWithOutSymbol * this.getMarkupMultiplier(), 2);
        },

        selectQty: function (priceItem) {
            if (priceItem === 'custom') {
                this.selectedQty('custom');
            } else {
                this.selectedCustomRunSize(0);
                this.selectedLowerRunSize(0);
                this.customRunSizeErrorMessage('');
                this.setCustomRunSizePrices(0.00, 0.00, 0, priceItem.qty);
            }
        },

        escapeSelect: function () {
            return false;
        },

        isSelectedQty: function (priceItem) {
            return !this.isCustomRunSizeSelected() && this.selectedQty() === priceItem.qty;
        },

        formatPrice: function (price, precision) {
            return priceUtils.formatPrice(price, _.extend(this.priceFormat, {requiredPrecision: precision}));
        },

        getTotalPrice: function (priceItem) {
            return this.formatPrice(priceItem.price, 2);
        },

        getUnitPrice: function (priceItem) {
            return this.formatPrice(priceItem.price / priceItem.qty, 4);
        },

        getMarkupPrice: function (priceItem) {
            return this.formatPrice(priceItem.price * this.getMarkupMultiplier(), 2);
        },

        getSelectedPriceItem: function () {
            if (this.isCustomRunSizeSelected()) {
                return null;
            }

            return _.findWhere(this.packagePrices(), {qty: this.selectedQty()}) || null;
        },

        getSelectedRunSize: function () {
            var priceItem;

            if (this.isCustomRunSizeSelected()) {
                return String(this.selectedCustomRunSize());
            }

            priceItem = this.getSelectedPriceItem();

            return priceItem ? String(priceItem.qty) : '';
        },

        getSelectedUnitPrice: function () {
            var priceItem;

            if (this.isCustomRunSizeSelected()) {
                return this.customRunSizeUnitPrice();
            }

            priceItem = this.getSelectedPriceItem();

            return priceItem ? this.getUnitPrice(priceItem) : '';
        },

        getSelectedTotalPrice: function () {
            var priceItem;

            if (this.isCustomRunSizeSelected()) {
                return this.customRunSizeTotalPrice();
            }

            priceItem = this.getSelectedPriceItem();

            return priceItem ? this.getTotalPrice(priceItem) : '';
        },

        getSelectedMarkupPrice: function () {
            var priceItem;

            if (this.isCustomRunSizeSelected()) {
                return this.customRunSizeMarkUpPrice();
            }

            priceItem = this.getSelectedPriceItem();

            return priceItem ? this.getMarkupPrice(priceItem) : '';
        },

        getMarkupMultiplier: function () {
            var multiplier = parseFloat(this.markupInterest());

            if (multiplier <= 0 || _.isNaN(multiplier)) {
                multiplier = 0;
            }

            return (multiplier + 100)/100;
        },

        isCollapsable: function () {
            return this.filteredPackagePrices().length > this.lineLimitThreshold;
        },

        isExpanded: function () {
            return this.lineLimit() > this.lineLimitThreshold;
        },

        collapsibleToggle: function () {
            if (this.isExpanded()) {
                this.showLess();
            } else {
                this.showMore();
            }
        },

        showMore: function () {
            this.lineLimit(this.filteredPackagePrices().length);
        },

        showLess: function () {
            this.lineLimit(this.lineLimitThreshold);
        }
    });
});
