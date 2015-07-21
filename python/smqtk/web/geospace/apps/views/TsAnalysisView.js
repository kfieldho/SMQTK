var tempus = tempus || {};

tempus.TsAnalysisView = Backbone.View.extend({
    el: '#ts-analysis-overlay',
    events: {
        'change #ts-analysis-grouping': 'changeGrouping'
    },

    initialize: function(options) {
        this.groupedBy = 'monthly';
        // The data needs to be grouped for the first rendering
        this.model.groupedBy(this.groupedBy);

        // Add event listener for future changes to display data
        this.model.on('change:tsDisplayData', this.render, this);

        $('#ts-analysis-overlay').draggable();

        this.similaritiesSummaryTemplate = _.template($('#similarities-summary-template').html());

        this.render();
    },

    changeGrouping: function(event) {
        this.groupedBy = $(event.currentTarget).find('option:selected').val();
        this.model.groupedBy(this.groupedBy);
    },

    focus: function () {
        var X_SHIFT = function(xCoord) {
            return xCoord - 3;
        };
        var minsMaxes = tempus.msaCollection.aggregateBoundingBox(
            [this.model.get('location')].concat(this.model.get('similarModels')));

        tempus.map.bounds({
            lowerLeft: [X_SHIFT(minsMaxes[0][0]), minsMaxes[1][0]],
            upperRight: [X_SHIFT(minsMaxes[0][1]), minsMaxes[1][1]]
        });
    },

    clear: function() {
        tempus.mapView.clearMsaFeatureLayer();

        if (!_.isUndefined(this.dateRangePicker)) {
            this.dateRangePicker.remove();
        }
    },

    render: function() {
        var _this = this;

        this.clear();

        var similarityHtml = this.similaritiesSummaryTemplate({
            msa: this.model.get('location').get('name'),
            similarMsas: _.invoke(this.model.get('similarModels'), 'get', 'name')
        });


        tempus.formView.$el.find('#similarities-summary').html(similarityHtml);

        // Create primary MSA outline
        tempus.formView.createMsaView(this.model.get('location'),
                                      function(shape) {
                                          shape.features[0].properties.strokeColor = '#d62728';
                                          shape.features[0].properties.strokeWidth = 3;
                                          return shape;
                                      });

        // Create similar MSA outlines
        _.each(this.model.get('similarModels'), function(model) {
            tempus.formView.createMsaView(model.get('name'),
                                          function(shape) {
                                              shape.features[0].properties.strokeColor = '#ff7f0e';
                                              shape.features[0].properties.strokeWidth = 3;
                                              return shape;
                                          });
        });

        // Focus on bounding box of MSAs
        this.focus();

        // Draw the time series and save the function for updating
        if (!_.isEmpty(this.model.get('tsDisplayData'))) {
            // Setup group picker, time range picker
            this.$el.find('#ts-analysis-overlay-options').html(
                _.template($('#ts-analysis-overlay-options-template').html())({
                    selected: this.groupedBy
            }));

            var dateExtent = this.model.dateExtent();
            this.dateRangePicker = $('#ts-analysis-overlay input[name="daterangepicker"]').daterangepicker({
                startDate: dateExtent[0],
                endDate: dateExtent[1]
            }, function(start, end) {
                _this.model.spanningDate(start, end, _this.groupedBy);
            });

            // Draw (or redraw) graph
            if (_.isFunction(this.redraw)) {
                this.redraw(this.model.get('tsDisplayData'));
            } else {
                // Draw the graph with the data
                var opts = _.merge(
                    {
                        // @todo more selector nonsense
                        selector: '#ts-analysis-overlay .plot',
                        x: 'date',
                        y: 'value'},
                    {
                        datasets: this.model.get('tsDisplayData')
                    });

                this.redraw = tempus.d3TimeSeries(opts);
            }
        }
    }
});
