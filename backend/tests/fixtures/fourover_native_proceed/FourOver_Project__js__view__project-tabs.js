/**
 * Created by FindCanary, LLC.
 * Project: 4over
 */

define([
    'jquery',
    'underscore',
    'uiComponent',
    'FourOver_Project/js/model/project',
    'FourOver_Project/js/model/selected-runsize',
    'FourOver_ProductRunSize/js/model/selected-custom-run-size',
    'FourOver_ProductRunSize/js/model/selected-lower-run-size',
    'FourOver_Project/js/model/project-messages',
    'FourOver_Project/js/model/project-tabs/product-options-validator',
    'FourOver_Project/js/action/reset-hash',
    'FourOver_Project/js/action/project/is-project-in-cart',
    'FourOver_Project/js/model/project-manager',
    'mage/translate',
    'mage/url',
    'mage/validation'
], function ($, _, Component, project, selectedQty, selectedCustomRunSize, selectedLowerRunSize, projectMessages, productOptionsValidator, resetHash, isProjectInCart, projectManager, $t, url) {
    'use strict';

    const defaultResetButton = $t('Start Over');

    return Component.extend({
        defaults: {
            template: 'FourOver_Project/project-tabs',
            optionsFormData: '#product_addtocart_form',
            selectedQty: selectedQty,
            selectedCustomRunSize: selectedCustomRunSize,
            selectedLowerRunSize: selectedLowerRunSize,
            projectTabs: '.product-options-tabs',
            projectMain: '.page-main',
            resetButton: defaultResetButton,
            canResetProject: true,
            isLoading: true,
            isProjectNameInvalid: false,
            tabsData: [],
            selectedTab: '',
            buttonLabel: '',
            nextStepInfo: '',
            shippingAndSetsTab: '',
            guestShippingRestricted: false,
            guestLoginUrl: '',
            stepItem: '.step-item',
            stepHeader: '.step-header',
            useClassicTabs: false,
            exports: {
                isProjectNameInvalid: 'project-name:isProjectNameInvalid'
            }
        },

        initialize: function () {
            this._super();

            if (this.useClassicTabs) {
                this.template = 'FourOver_Project/project-tabs-classic';
            }

            this.selectedTab.subscribe(function (newValue) {
                $(this.projectTabs).find('.data.item.content').removeClass('active');
                $('[data-tab-id="' + newValue + '"').addClass('active');
                if (!this.useClassicTabs) {
                    this.syncStepHeaders();
                }
            }.bind(this));

            if (!this.selectedTab()) {
                this.selectedTab(this.tabsData[0].alias);
            }

            var isAddressTabPresent = _.filter(this.tabsData, function (tab) {
                return tab.alias === 'product.addresses'
            }).length;

            if (!this.shippingAndSetsTab && this.tabsData.length) {
                this.shippingAndSetsTab = this.tabsData[this.tabsData.length - 1].alias;
            }

            if (!this.useClassicTabs) {
                this.initAccordion();
            }

            $('.proceed-to-shipping').on('click', function () {
                if (this.guestShippingRestricted) {
                    window.location.href = this.getGuestLoginUrl();
                    return;
                }
                if ((this.selectedCustomRunSize() != '' || this.selectedQty() === 'custom') && this.selectedLowerRunSize() == '') {
                    var $runSizes = $('.product-options-wrapper .runsizes-table').first();

                    this.scrollToElement($runSizes.length ? $runSizes : $('.product-options-wrapper'));
                    return;
                }
                this.validateTurnAroundTimeValue();
                this.setNextActiveTab();
            }.bind(this));

            if (!isAddressTabPresent) {
                this.isLoading(false);
            }

            $(this.projectTabs).addClass('initialized');

            $(this.optionsFormData).off('submit').on('submit', function (e) {
                e.preventDefault();

                if (this.guestShippingRestricted) {
                    return false;
                }

                this.setNextActiveTab();
                return false;
            }.bind(this));

            $(this.optionsFormData).off('keydown.enterGuard').on('keydown.enterGuard', 'input', function (e) {
                if (e.key === 'Enter' && !/^(button|submit|reset|image|file)$/.test(e.target.type)) {
                    e.preventDefault();
                }
            });
        },

        initObservable: function () {
            this._super();
            this.observe([
                'isLoading',
                'resetButton',
                'canResetProject',
                'selectedTab',
                'isProjectNameInvalid'
            ]);

            return this;
        },

        initAccordion: function () {
            var self = this;

            $(this.projectTabs).on('click', this.stepHeader, function (event) {
                event.preventDefault();
                self.editStep($(this).data('step-target'));
            });

            this.syncStepHeaders();
        },

        getStepIndex: function (alias) {
            return _.map(this.tabsData, function (tab) {
                return tab.alias;
            }).indexOf(alias);
        },

        isStepComplete: function (alias) {
            var index = this.getStepIndex(alias);

            if (index < 0) {
                return false;
            }

            return index < this.getStepIndex(this.selectedTab());
        },

        isStepReachable: function (alias) {
            var index = this.getStepIndex(alias),
                current = this.getStepIndex(this.selectedTab());

            if (index < 0 || this.isGuestRestrictedTab(alias)) {
                return false;
            }

            return index <= current + 1;
        },

        canEnterStep: function (alias) {
            if (!this.isStepReachable(alias)) {
                return false;
            }

            if (this.getStepIndex(alias) <= this.getStepIndex(this.selectedTab())) {
                return true;
            }

            return productOptionsValidator.isValid();
        },

        editStep: function (alias) {
            if (!this.canEnterStep(alias)) {
                return;
            }

            this.setActiveTab(alias);
        },

        syncStepHeaders: function () {
            var self = this,
                selected = this.selectedTab();

            $(this.projectTabs).find(this.stepItem).each(function () {
                var $item = $(this),
                    alias = $item.data('step-id'),
                    isActive = alias === selected,
                    isComplete = self.isStepComplete(alias);

                $item.toggleClass('_active', isActive)
                    .toggleClass('_complete', isComplete)
                    .toggleClass('_disabled', !isActive && !self.isStepReachable(alias));

                $item.find(self.stepHeader).attr('aria-expanded', isActive ? 'true' : 'false');
            });
        },

        getProductOptionsTab: function () {
            return this.productOptionsTab
                || (this.tabsData[0] && this.tabsData[0].alias)
                || 'product.configuration';
        },

        isGuestRestrictedTab: function (tabAlias) {
            return !!this.guestShippingRestricted
                && !!this.shippingAndSetsTab
                && tabAlias === this.shippingAndSetsTab;
        },

        getGuestLoginUrl: function () {
            return this.guestLoginUrl
                || (window.hopkinsGuestDtePdp && window.hopkinsGuestDtePdp.loginUrl)
                || url.build('customer/account/login');
        },

        setActiveTab: function (tabAlias) {
            if (this.isGuestRestrictedTab(tabAlias)) {
                return;
            }

            if (project().state === 'cart' && !isProjectInCart(project())) {
                this.regenerateProjectUuids();
            }

            if (tabAlias === this.selectedTab()
                || (this.selectedTab() === this.getProductOptionsTab() && !productOptionsValidator.isValid())
            ) {
                return;
            }

            var selectedTabSelector = $('[data-tab-id="' + this.selectedTab() + '"]');
            if (this.tabsData[this.getNextTabIndex()] && selectedTabSelector.find('#project_name').length) {
                this.validateProjectName();

                if (this.isProjectNameInvalid()) {
                    return
                }
            }

            this.selectedTab(tabAlias);
        },

        setPreviousActiveTab: function () {
            this.goToTab(this.tabsData[this.getPreviousTabIndex()].alias);
        },

        getPreviousTabIndex: function () {
            var currentIndex = _.map(this.tabsData, function(tab) { return tab.alias; }).indexOf(this.selectedTab());
            return --currentIndex
        },

        setNextActiveTab: function () {
            this.goToTab(this.tabsData[this.getNextTabIndex()].alias);
        },

        goToTab: function (tabAlias) {
            var $error;

            this.setActiveTab(tabAlias);

            if (this.selectedTab() === tabAlias) {
                this.scrollToStep(tabAlias);
                return;
            }

            $error = $(this.projectTabs).find('.mage-error:visible').first();

            if ($error.length) {
                this.scrollToElement($error.closest('.field').length ? $error.closest('.field') : $error);
            }
        },

        scrollToStep: function (tabAlias) {
            var $step = this.useClassicTabs
                ? $(this.projectTabs)
                : $(this.projectTabs).find(this.stepItem + '[data-step-id="' + tabAlias + '"]');

            this.scrollToElement($step.length ? $step : $(this.projectTabs));
        },

        scrollToElement: function ($target) {
            var top;

            if (!$target || !$target.length || !$target.is(':visible')) {
                return;
            }

            top = $target.offset().top - 10;

            if (top < $(window).scrollTop()) {
                top -= $('.page-header').outerHeight() || 0;
            }

            $('html, body').animate({'scrollTop': Math.max(top, 0)}, 300);
        },

        getNextTabIndex: function () {
            var currentIndex = _.map(this.tabsData, function(tab) { return tab.alias; }).indexOf(this.selectedTab());
            return ++currentIndex
        },

        scrollTo: function (target) {
            var scrollTopPos = target
                ? $(target).offset().top
                : $(this.projectMain).offset().top;
            $('html, body').animate({'scrollTop': scrollTopPos}, 300);
        },

        resetProject: function (scrollTarget) {
            this.setActiveTab(this.tabsData[0].alias);
            this.resetProductOptions();
            this.scrollTo(scrollTarget);
            this.trigger('reset-project');
            this.resetButton(defaultResetButton);
            $(window).trigger('project-reset-requested');
            resetHash();
        },

        regenerateProjectUuids: function () {
            var error = {
                message: $t('You are editing project that is already processed, it was regenerated for you.')
            };
            projectMessages.addErrorMessage(error);
            projectManager.resetUuid();
            this.scrollTo();
            this.trigger('reset-project-uuid');
            this.resetButton(defaultResetButton);
            resetHash();
        },

        resetProjectBtnAction: function (scrollTarget) {
            if (project().state === 'cart') {
                window.location.href = url.build('checkout/cart');
                return;
            }

            this.resetProject(scrollTarget)
            projectMessages.clear();
        },

        resetProductOptions: function () {
            $(this.optionsFormData).validation('clearError').find('input, select').each(function () {
                var $element = $(this);

                if ($element.hasClass('product-4over-input') || $element.is(':hidden')) {
                    return;
                }

                if ($element.is(':checkbox')) {
                    $element.prop('checked', false);
                } else {
                    $element.hasClass('swatch-select') ? $element.val('0') : $element.val('');
                }
            });

            if ($('.swatch-opt').length) {
                $('.swatch-attribute-selected-option').text('');
                $('.swatch-option.selected').removeClass('selected');
                $('.swatch-option.disabled').removeClass('disabled').removeAttr('disabled');
                $('.swatch-select').removeClass('mage-error');
                $('.swatch-select option').removeClass('disabled').removeAttr('disabled');
                $('.swatch-attribute').removeClass('swatch-hidden');
            } else {
                $('.super-attribute-select').filter(':first').val('').trigger('change');
            }
        },

        validateTurnAroundTimeValue: function () {
            var $tatInput = $('input[name="turnaround_time"]:checked');
            var selectedTat = $tatInput.length ? $tatInput.val() : undefined;

            if (typeof selectedTat !== 'undefined' && selectedTat !== '' &&
                typeof project === 'function' && project() && project().productOptions()
            ) {
                project().productOptions().turnaround_time = selectedTat;
            }
        },

        validateProjectName: function () {
            let name = project().name() || '';
            this.isProjectNameInvalid(!name.trim());
            this.isProjectNameInvalid.valueHasMutated();
        }
    });
});
