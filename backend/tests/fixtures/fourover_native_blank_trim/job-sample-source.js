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
    'FourOver_JobSampleTotal/js/model/product/job-sample',
    'mage/translate'
], function (ko, Component, project, productOptionsManager, selectedRunsize, jobSample) {
    'use strict';

    return Component.extend({
        defaults: {
            template: 'FourOver_JobSampleTotal/project/job-sample',
            jobSample: jobSample,
            isVisible: false,
            formatedPrice: '$9.99',
            price: 9.99,
            imports: {
                priceFormat: 'project:priceFormat',
                resetSelectedOption: 'project-tabs:reset-project',
                renderTatPrice: 'project:qtySelectStatus'
            },
            exports: {
                saveJobSample: 'project:saveProductOptions',
            }
        },

        initObservable: function () {
            this._super();
            this.observe([
                'isVisible',
                'saveJobSample'
            ]);

            this.jobSample.subscribe(function (value) {
                if (value) {
                    jobSample(this.price)
                } else {
                    jobSample(0);
                }
                this.saveJobSample.valueHasMutated();
            }, this);

            return this;
        },

        resetSelectedOption: function () {
            this.jobSample('');
        }
    });
});
