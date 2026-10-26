/**
 * Created by FindCanary, LLC.
 * Project: 4over
 */

define([
    'uiComponent',
    'FourOver_Project/js/model/project'
], function (Component, project) {
    'use strict';

    return Component.extend({
        defaults: {
            template: 'FourOver_Project/project/name',
            projectName: project().name,
            isShippingAndSetsActive: false,
            isProjectNameInvalid: false,
            imports: {
                isShippingAndSetsActive: 'project:isShippingAndSetsActive'
            },
            isPrintReadyEnabled: false,
            tooltipImageUrl: ''
        },

        initialize: function (config) {
            this._super();

            if (config && typeof config.isPrintReadyEnabled !== 'undefined') {
                this.isPrintReadyEnabled = config.isPrintReadyEnabled;
                this.tooltipImageUrl = config.tooltipImageUrl ?? '';
            }

            return this;
        },

        initObservable: function () {
            this._super();
            this.observe([
                'isShippingAndSetsActive',
                'isProjectNameInvalid'
            ]);
            this.isProjectNameInvalid.extend({notify: 'always'});

            this.isShippingAndSetsActive.subscribe(function (isActive) {
                if (isActive) {
                    this.validateProjectName();
                }
            }, this);

            project().name.subscribe(function () {
                this.validateProjectName();
            }.bind(this));

            return this
        },

        validateProjectName: function () {
            let name = project().name() || '';
            this.isProjectNameInvalid(!name.trim());
        }
    });
});
