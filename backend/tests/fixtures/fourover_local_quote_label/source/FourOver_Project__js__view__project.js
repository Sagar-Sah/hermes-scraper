/**
 * Created by FindCanary, LLC.
 * Project: 4over
 */

define([
    'underscore',
    'jquery',
    'ko',
    'uiComponent',
    'FourOver_Project/js/model/project',
    'FourOver_Project/js/model/project-manager',
    'FourOver_Project/js/model/project/address-manager',
    'FourOver_ProductCustomOptions/js/model/custom-option',
    'FourOver_Project/js/model/product-options-manager',
    'Magento_ConfigurableProduct/js/configurable'
], function (_, $, ko, Component, project, projectManager, projectAddressManager, customOption, productOptionsManager) {
    'use strict';

    return Component.extend({
        defaults: {
            template: 'FourOver_Project/project',
            optionsFormData: '#product_addtocart_form',
            isLoading: false,
            priceFormat: {},
            projectData: {},
            projectName: project().name,
            selectedTab: '',
            shippingAndSetsTab: '',
            qtySelectStatus: false,
            links: {
                selectedTab: 'project-tabs:selectedTab'
            },
            imports: {
                resetProject: 'project-tabs:reset-project',
                shippingAndSetsTab: 'project-tabs:shippingAndSetsTab'
            },
            statefull: {
                projectData: true
            },
            tracks: {
                projectData: true
            }
        },

        initialize: function () {
            this._super();

            if (!_.isEmpty(this.projectData)) {
                projectManager.setProjectData(this.projectData);
            }

            this.initializeProductOptions();
            this.initializeShipmentAddresses();
        },

        initObservable: function () {
            this._super();
            this.observe([
                'isLoading',
                'selectedTab',
                'qtySelectStatus'
            ]);
            this.isShippingAndSetsActive = ko.pureComputed(function () {
                return this.shippingAndSetsTab === this.selectedTab();
            }, this);

            var projectSaveTimeout = null;
            project.subscribe(function () {
                clearTimeout(projectSaveTimeout);
                projectSaveTimeout = setTimeout(this.saveProject.bind(this), 500);
            }, this);

            return this;
        },

        initializeProductOptions: function () {
            var productOptions = _.clone(project().productOptions());
            if (productOptions) {
                productOptionsManager.apply(productOptions);
            }

            $(document).on('click', '.swatch-option', function () {
                customOption.updatePrices(this.priceFormat);
                this.saveProductOptions();
            }.bind(this));
            $('.super-attribute-select').on('change', function () {
                customOption.updatePrices(this.priceFormat);
                this.saveProductOptions();
            }.bind(this));
            $(document).on('change', '.swatch-select, .product-4over-input, .super-attribute-select', function () {
                customOption.updatePrices(this.priceFormat);
                this.saveProductOptions();
            }.bind(this));
            $('.product-custom-option').change(function () {
                customOption.updatePrices(this.priceFormat);
                this.saveProductOptions();
            }.bind(this));

            $('#tabs-loader').hide();

            // Use this event to update product options data
            $(document).on('save-product-options', this.saveProductOptions.bind(this));

            $(document).on('click', 'label[for^="attribute"]', function (e) {
                let targetSelect = $('#' + $(this).attr('for'));
                if (targetSelect.hasClass('hide-select-dropdown')) {
                    e.preventDefault();
                    e.stopPropagation();
                    return false;
                }
            });
        },

        saveProductOptions: function () {
            project().productId = productOptionsManager.getProductId();
            project().productOptions(productOptionsManager.parse());
        },

        saveProject: function () {
            this.projectData = projectManager.getProjectData(true);
        },

        initializeShipmentAddresses: function () {
            var shippingAddressIds = [];
            _.each(project().shipments(), function (shipment) {
                if (shipment.shippingAddress() && shipment.shippingAddress().id) {
                    shippingAddressIds.push(shipment.shippingAddress().id);
                }
                if (shipment.dropShipAddress() && shipment.dropShipAddress().id) {
                    shippingAddressIds.push(shipment.dropShipAddress().id);
                }
            });

            projectAddressManager.loadProjectAddresses(shippingAddressIds)
                .done(function () {
                    this.updateShipmentAddresses();
                }.bind(this));
        },

        updateShipmentAddresses: function () {
            _.each(project().shipments(), function (shipment) {
                var shippingAddress = shipment.shippingAddress();
                shippingAddress = !_.isEmpty(shippingAddress) ? projectAddressManager.getAddressById(shippingAddress.id) : null;
                shippingAddress = shippingAddress || projectAddressManager.getDefaultShipping();
                shipment.shippingAddress(shippingAddress);

                var dropShipAddress = shipment.dropShipAddress();
                dropShipAddress = !_.isEmpty(dropShipAddress) ? projectAddressManager.getAddressById(dropShipAddress.id) : null;
                shipment.dropShipAddress(dropShipAddress);
            });
        },

        resetProject: function () {
            projectManager.reset();
            this.updateShipmentAddresses();
        },

        cleanProjectData: function () {
            this.projectData = _.pick(this.projectData, 'product_options', 'product_id', 'run_size');
        }
    });
});
