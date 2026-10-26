/**
 * Created by FindCanary, LLC.
 * Project: 4over
 */

define([
    'jquery',
    'underscore',
    'ko',
    'uiComponent',
    'FourOver_Project/js/model/product-selected-id',
    'FourOver_Project/js/model/selected-runsize',
    'FourOver_ProductRunSize/js/model/selected-custom-run-size',
    'FourOver_TurnaroundTime/js/model/product/turnaround-time',
    'FourOver_Project/js/model/product-price-processor',
    'FourOver_TurnaroundTime/js/model/product/tat-price-percent',
    'FourOver_TurnaroundTime/js/model/product/preselect-tat',
    'priceUtils',
    'mage/url',
    'mage/translate'
], function ($, _, ko, Component, productSelectedId,  selectedRunsize, selectedCustomRunSize, productTat, productPriceProcessor, tatPricePercent, preselectTAT, priceUtils, url, $t) {
    'use strict';

    return Component.extend({
        defaults: {
            template: 'FourOver_TurnaroundTime/product/turnaround-time',
            isVisible: false,
            preselectTat: preselectTAT,
            globalTatPriceConfig: {},
            tatPrices: {},
            tatOptionLabels: {},
            tatOptionOrder: {},
            defaultTatOptions: [],
            productTatOptions: [],
            defaultAvailableOptions: {},
            productAvailableOptions: {},
            tatOptions: [],
            selectedTat: productTat,
            imports: {
                priceFormat: 'project:priceFormat',
                resetSelectedOption: 'project-tabs:reset-project',
                renderTatPrice: 'project:qtySelectStatus'
            }
        },

        initialize: function () {
            this._super();

            this.buildTatDropdown(productSelectedId(), selectedRunsize());
            this.updateTatPricePercent();
            this.selectedTat.subscribe(() => {
                $(document).trigger('save-product-options');
            })
        },

        initObservable: function () {
            this._super();
            this.observe([
                'isVisible',
                'tatOptions'
            ]);

            this.selectedTat.subscribe(function (tatId) {
                tatPricePercent(this.tatPrices[tatId]);
            }, this);

            productSelectedId.subscribe(function (productId) {
                this.renderTatPrice(productId, selectedRunsize());
            }, this);
            selectedRunsize.subscribe(function (qty) {
                this.renderTatPrice(productSelectedId(), qty);
            }, this);

            selectedCustomRunSize.subscribe((quantities) => {
                let qty = selectedRunsize();
                this.renderTatPrice(productSelectedId(), qty);
            });

            return this;
        },

        getTatCmsUrl: function () {
            return url.build('turnaround-times');
        },

        renderTatPrice: function (productId, qty) {
            // Avoid passing boolean value as a product id
            productId = productId && typeof productId !== 'boolean'
                ? productId
                : productSelectedId();
            qty = qty || selectedRunsize();

            this.buildTatDropdown(productId, qty);
            this.updateTatPricePercent();
        },

        updateTatPricePercent: function () {
            if (this.tatPrices[productTat()]) {
                tatPricePercent(this.tatPrices[productTat()]);
            }
        },

        buildTatDropdown: function (productId, qty) {
            if (qty < 1) return false;
            var productTatOptions = this.getProductTatOptions(productId);

            var itemTotalPrice = productPriceProcessor.getItemTotalPrice(qty);

            var tatDropdown = [];
            var tatOptions = [];
            this.tatPrices = {};
            let availableTatOptions;

            if (itemTotalPrice > 0) {
                if (!_.isEmpty(productTatOptions)) {
                    tatOptions = productTatOptions;
                } else {
                    tatOptions = this.globalTatPriceConfig;
                }

                availableTatOptions = this.getAvailableTatOptions(productId, qty);

                _.each(tatOptions, function (tatOption, tatType) {
                    var labelParts = [this.tatOptionLabels[tatType]];
                    if (availableTatOptions && availableTatOptions.includes(tatType)) {
                        if (tatOptions[tatType] > 0) {
                            var tatOptionPrice = itemTotalPrice * tatOptions[tatType]/100;
                            labelParts.push(priceUtils.formatPrice(tatOptionPrice, this.priceFormat));
                        } else if (this.preselectTat()) { // preselect faster free tat
                            this.selectedTat(tatType);
                            this.preselectTat(false);
                        }

                        tatDropdown.push({
                            'value': tatType,
                            'label': labelParts.join(' +'),
                            'sort_order': this.tatOptionOrder[tatType]
                        });
                        this.tatPrices[tatType] = tatOptions[tatType] || 0;
                    }

                }, this);
            }

            tatDropdown = _.sortBy(tatDropdown, 'sort_order');
            this.tatOptions(tatDropdown);
            this.isVisible(tatDropdown.length > 0);

            // Preselect any first tat if there are no free options on first load
            if (tatDropdown.length > 0 && !productTat() && this.preselectTat()) {
                this.selectedTat(tatDropdown[0]['value']);
            }

            // Reset checkboxes if the previously selected tat is no more available
            if (availableTatOptions && !availableTatOptions.includes(this.selectedTat())) {
                this.resetSelectedOption();
            }
        },

        getProductTatOptions: function (productId) {
            return !_.isEmpty(this.productTatOptions[productId]) ? this.productTatOptions[productId] : this.defaultTatOptions;
        },

        getAvailableTatOptions: function (productId, qty) {
            var availableTatOptions = !_.isEmpty(this.productAvailableOptions[productId]) ? this.productAvailableOptions[productId] : this.defaultAvailableOptions;
            var applicableTatQtys = _.keys(availableTatOptions);
            var applicableTatOptions = _.filter(applicableTatQtys, function (tatQty) { return tatQty*1 >= qty*1; });
            var tatOptionQty = _.min(applicableTatOptions, function (tatQty) { return tatQty*1; });

            return availableTatOptions[tatOptionQty];
        },

        resetSelectedOption: function () {
            this.selectedTat('');
        }
    });
});
