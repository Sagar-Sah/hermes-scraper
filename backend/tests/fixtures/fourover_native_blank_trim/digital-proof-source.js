/**
 * Created by FindCanary, LLC.
 * Project: 4over
 */

define([
    'ko',
    'uiComponent',
    'FourOver_Project/js/model/project',
    'FourOver_Project/js/model/product-options-manager',
    'FourOver_Project/js/model/selected-runsize',
    'FourOver_DigitalProof/js/model/product/digital-proof',
    'mage/translate'
], function (ko, Component, project, productOptionsManager, selectedRunsize, digitalProof) {
    'use strict';

    return Component.extend({
        defaults: {
            template: 'FourOver_DigitalProof/project/digital-proof',
            digitalProof: digitalProof,
            isVisible: false,
            formatedPrice: '$5.00',
            price: 5,
            imports: {
                priceFormat: 'project:priceFormat',
                resetSelectedOption: 'project-tabs:reset-project',
                renderTatPrice: 'project:qtySelectStatus'
            },
            exports: {
                saveDigitalProof: 'project:saveProductOptions',
            }
        },

        initObservable: function () {
            this._super();
            this.observe([
                'isVisible',
                'saveDigitalProof'
            ]);

            this.digitalProof.subscribe(function (value) {
                if (value) {
                    digitalProof(this.price)
                } else {
                    digitalProof(0);
                }
                this.saveDigitalProof.valueHasMutated();
            }, this);

            return this;
        },

        resetSelectedOption: function () {
            this.digitalProof('');
        }
    });
});
