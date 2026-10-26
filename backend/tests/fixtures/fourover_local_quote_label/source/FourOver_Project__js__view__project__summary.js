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
    'FourOver_Project/js/model/product-selected-id',
    'FourOver_Project/js/model/project-manager',
    'FourOver_Project/js/model/project/shipment/address-manager',
    'FourOver_Project/js/model/project/reward-points',
    'FourOver_Project/js/action/project/to-cart',
    'FourOver_Project/js/action/project/post-request',
    'FourOver_Project/js/action/project/is-project-in-cart',
    'FourOver_Project/js/model/project/artwork-state',
    'FourOver_ProductStepConfig/js/model/config',
    'priceUtils',
    'mage/translate'
], function (_, $, ko, Component, project, productSelectedId, projectManager, shipmentAddressManager, projectRewardPoints, projectToCart, postRequest, isProjectInCart, artworkState, productStepConfig, priceUtils, $t) {
    'use strict';

    var defaultAddToCartButton = $t('Add to Cart');
    var defaultAddToCartMessage = $t('Project %projectName successfully added to the shopping cart.');

    var hashString = function (s) {
        for (var i = 0, h = 0; i < s.length; i++)
            h = Math.imul(31, h) + s.charCodeAt(i) | 0;
        return h + '';
    };

    return Component.extend({
        defaults: {
            template: 'FourOver_Project/project/summary',
            projectSummaryUrl: '',
            projectToArtUrl: '',
            shoppingCartUrl: '',
            isLoading: false,
            isProjectLoading: false,
            isShippingAndSetsActive: false,
            isSelectedShippingMethods: false,
            isProjectNameInvalid: false,
            allowAddToCart: false,
            summaryOptions: [],
            projectTotals: {},
            priceFormat: {},
            addToCartButton: defaultAddToCartButton,
            addToCartMessage: defaultAddToCartMessage,
            artworkMode: '',
            enabledDesignerProductIds: [],
            artworkStatus: '',
            isGroupShippingApplied: false,
            isClassicPdp: false,
            imports: {
                priceFormat: 'project:priceFormat',
                isShippingAndSetsActive: 'project:isShippingAndSetsActive',
                resetProject: 'project-tabs:reset-project',
                resetProjectUuids: 'project-tabs:reset-project-uuid'
            },
            exports: {
                regenerateProjectUuids: 'project-tabs:regenerateProjectUuids',
                cleanProjectData: 'project:cleanProjectData'
            },
            links: {
                isProjectLoading: 'project:isLoading'
            }
        },

        initObservable: function () {
            this._super();
            this.observe([
                'isLoading',
                'isProjectLoading',
                'isShippingAndSetsActive',
                'isSelectedShippingMethods',
                'isProjectNameInvalid',
                'addToCartButton',
                'addToCartMessage',
                'cleanProjectData',
                'summaryOptions',
                'projectTotals',
                'allowAddToCart',
                'artworkStatus',
                'regenerateProjectUuids',
                'isGroupShippingApplied'
            ]);

            var totalsTimeout = null;
            project.subscribe(function () {
                if (this.isShippingAndSetsActive()) {
                    clearTimeout(totalsTimeout);
                    totalsTimeout = setTimeout(this.refreshTotals.bind(this), 500);
                }
            }, this);

            this.isShippingAndSetsActive.subscribe(function (isActive) {
                if (isActive) {
                    this.refreshTotals();
                    this.validateProjectName();
                    this.validateShippingMethod(project().shipments());
                }
            }, this);

            this.initializeArtworkStatus();

            this.updateAddToCartState();

            this.isClassicPdp = document.body.classList.contains('pdp-classic-tabs');

            this.artworkStatus.subscribe(this.updateAddToCartState, this);

            project().shipments.subscribe(this.validateShippingMethod, this);
            project().shipments.subscribe(this.validateGroupShippingApplied, this);
            project().shipments.subscribe(_.debounce(this.validateArtworkStatus, 100), this);

            let projectOptions = _.omit(project().productOptions(), 'eddm', 'direct_mail');
            project().productOptions.subscribe(function () {
                let newProjectOptions = _.omit(project().productOptions(), 'eddm', 'direct_mail');
                if (!_.isEqual(projectOptions, newProjectOptions)) {
                    this.artworkStatus('')
                }
                projectOptions = newProjectOptions;
                projectRewardPoints(null);
            }.bind(this));

            project().name.subscribe(function () {
                if (this.isShippingAndSetsActive()) {
                    this.validateProjectName()
                }
            }, this);

            window.addEventListener('message', this.eventListener.bind(this));

            return this;
        },

        refreshTotals: function () {
            var projectKey = this.getProjectKey();
            if (!this.projectTotals()[projectKey]) {
                this.requestTotals();
            }
        },

        requestTotals: function () {
            this.isLoading(true);

            var projectKey = this.getProjectKey();
            var projectOptionsKey = this.getProjectOptionsKey();

            postRequest(
                this.projectSummaryUrl,
                projectManager.getProjectData()
            ).done(function (response) {
                    this.summaryOptions()[projectOptionsKey] = response['product_options'];
                    this.summaryOptions.valueHasMutated();
                    this.projectTotals()[projectKey] = response['totals'];
                    this.projectTotals.valueHasMutated();
                    projectRewardPoints(response['reward_points'] || null);
                }.bind(this)
            ).always(function () {
                    this.isLoading(false);
                }.bind(this)
            );
        },

        validateShippingMethod: function (shipments) {
            var selectedAll = true;
            _.each(shipments, function (shipment) {
                if (!shipment.shippingMethod() || !shipment.shippingAddress()) {
                    selectedAll = false;
                }
            });
            this.isSelectedShippingMethods(selectedAll);
        },

        validateGroupShippingApplied: function (shipments) {
            var groupShippingApplied = _.every(shipments, function (shipment) {
                return shipment.isGroupShippingApplied();
            });
            this.isGroupShippingApplied(groupShippingApplied);
        },

        validateProjectName: function () {
            this.isProjectNameInvalid(!project().name())
        },

        initializeArtworkStatus: function () {
            this.artworkStatus(projectManager.getProjectData()['shipments'][0]['jobs'][0]['artwork_status']);
        },

        isArtworkStatusSubmitted: function (status) {
            let atStatus = status ? status : this.artworkStatus();

            return artworkState.isSatisfied(atStatus, this.useStrictArtworkStatuses());
        },

        useStrictArtworkStatuses: function () {
            return productStepConfig.isStrictArtworkStatuses();
        },

        isArtworkStatusFailed: function () {
            return this.useStrictArtworkStatuses() && artworkState.isFailed(this.artworkStatus());
        },

        validateArtworkStatus: function (shipments) {
            if (this.isArtworkStatusSubmitted()) {
                _.each(shipments, function (shipment) {
                    _.each(shipment.jobs(), function (job) {
                        if (!this.isArtworkStatusSubmitted(job.artworkStatus())) {
                            this.artworkStatus('Not Submitted');
                        }
                    }, this);
                }, this);
            }
        },

        getSummaryOptions: function () {
            let options = this.summaryOptions()[this.getProjectOptionsKey()] || [];
            const hasTrimSize = _.some(options, function(option) {
                return option.label === 'Trim Size';
            });
            if (hasTrimSize) {
                options = _.filter(options, function(option) {
                    return option.label !== 'Size';
                });
            }

            return options;
        },

        getTotals: function () {
            return this.projectTotals()[this.getProjectKey()];
        },

        getProjectOptionsKey: function () {
            return hashString(JSON.stringify(project().productOptions()));
        },

        getProjectKey: function () {
            var projectCacheKey = [this.getProjectOptionsKey()];
            _.each(project().shipments(), function (shipment) {
                if (!shipmentAddressManager.getAddressId(shipment)) {
                    return;
                }
                projectCacheKey.push(shipmentAddressManager.getRateCacheKey(shipment));
                projectCacheKey.push(shipment.shippingMethod());
            });
            return projectCacheKey.join('-');
        },

        resetProject: function () {
            this.artworkStatus('');
            this.addToCartButton(defaultAddToCartButton);
            this.addToCartMessage(defaultAddToCartMessage);
            this.allowAddToCart(this.artworkMode === 'na');
        },

        resetProjectUuids: function () {
            this.addToCartButton(defaultAddToCartButton);
            this.addToCartMessage(defaultAddToCartMessage);
        },

        updateAddToCartState: function () {
            this.allowAddToCart(false);

            if (this.artworkMode === 'na') {
                this.allowAddToCart(true);
            } else if (this.artworkMode === 'required' && this.isArtworkStatusSubmitted()) {
                this.allowAddToCart(true);
            } else if (this.artworkMode === 'default' && this.isArtworkStatusSubmitted()) {
                this.allowAddToCart(true);
            }
        },

        addToCart: function () {
            if (project().state === 'cart' && !isProjectInCart(project())) {
                this.regenerateProjectUuids.valueHasMutated();
            }

            this.isProjectLoading(true);
            $('#tabs-loader').show();

            postRequest(
                this.projectToArtUrl,
                projectManager.getProjectData()
            ).done(function () {
                projectToCart(
                    projectManager.getProjectData()
                ).done(function () {
                    $.cookieStorage.set(
                        'mage-messages',
                        [{
                            'type': 'success',
                            'text': this.addToCartMessage().replace('%projectName', project().name())
                        }]
                    );
                    this.cleanProjectData.valueHasMutated();
                    document.location.href = this.shoppingCartUrl;
                }.bind(this))
                    .fail(function () {
                        $('#tabs-loader').hide();
                    })
                    .always(function () {
                        this.isProjectLoading(false);
                    }.bind(this));
            }.bind(this))
                .fail(function () {
                    $('#tabs-loader').hide();
                });

        },

        uploadToArt: function () {
            this.openArtworkTool(false);
        },

        createDesign: function () {
            this.openArtworkTool(true);
        },

        openArtworkTool: function (useDesigner) {
            if (project().state === 'cart' && !isProjectInCart(project())) {
                this.regenerateProjectUuids.valueHasMutated();
            }

            this.isProjectLoading(true);

            postRequest(
                this.projectToArtUrl,
                projectManager.getProjectData()
            ).done(function () {
                    this.trigger('showArtworkPopup', useDesigner);
                }.bind(this)
            ).always(function () {
                    this.isProjectLoading(false);
                }.bind(this)
            );
        },

        isDesignerEnabled: function () {
            var configuredProductId = Number(productSelectedId() || project().productId);

            return _.contains(this.enabledDesignerProductIds, configuredProductId);
        },

        eventListener: function (messageEvent) {
            var artworkEvent = messageEvent.data || {};

            if (artworkEvent.type === 'ArtworkTool' && _.has(artworkEvent, 'status')) {
                this.artworkStatus(artworkEvent.status);
            }
        },

        formatPrice: function (price) {
            return priceUtils.formatPrice(price, this.priceFormat);
        }
    });
});
